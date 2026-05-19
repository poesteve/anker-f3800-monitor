"""Cloud MQTT monitor client for Anker Solix F3800.

Uses the unofficial anker-solix-api library to authenticate with Anker's cloud,
connect to the AWS IoT MQTT broker, and receive decoded F3800 telemetry data.

Correct initialization flow:
1. Authenticate with Anker Cloud API (async_authenticate)
2. Fetch sites & devices (update_sites) — populates api.devices cache
3. Start MQTT session (startMqttSession) — connects broker, registers internal callback
4. Register mqtt_update_callback — called with device_sn when new data arrives
5. Subscribe to device topics and start message poller
6. Send realtime trigger to request initial status

The library handles all MQTT message decoding internally:
  - Raw MQTT → mqtt_received() → update_device_mqtt() → devices[sn]["mqtt_data"]
  - Our mqtt_update_callback receives just the device_sn string
  - We read the decoded data from api.devices[sn]["mqtt_data"]

IMPORTANT: The mqtt_update_callback is invoked from paho-mqtt's background thread,
NOT the asyncio event loop thread. We use run_coroutine_threadsafe to safely
schedule async work on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from src.clients.base import BaseMonitor
from src.config import AppSettings
from src.models import F3800Data

logger = logging.getLogger(__name__)

# Minimum number of non-timestamp keys in mqtt_data before we consider
# the data "ready" to process. The F3800 sends sensor data across multiple
# MQTT messages (state_info, param_info), so the callback fires multiple
# times as data accumulates. We wait until sufficient keys are present.
_MIN_SENSOR_KEYS = 10

# Debounce window (seconds). After receiving an update, we wait this long
# for additional MQTT messages to arrive before processing the data.
_DEBOUNCE_SECONDS = 3.0


class CloudMqttMonitor(BaseMonitor):
    """Monitor F3800 via Anker Cloud API + MQTT.

    Uses the anker-solix-api library for authentication and MQTT communication.
    Requires internet access and Anker account credentials.
    """

    def __init__(self, config: AppSettings) -> None:
        super().__init__(config)
        self._api: Any = None
        self._mqtt_session: Any = None
        self._websession: Any = None
        self._device_info: dict = {}
        self._device_dict: dict = {}  # Full device dict for realtime_trigger
        self._poller_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Debounce state for mqtt_update_callback
        self._debounce_timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()
        # Periodic trigger state (event created in start() after loop is running)
        self._force_trigger_event: asyncio.Event | None = None
        self._last_trigger_time: float = 0.0
        self._poll_count: int = 0

    async def start(self) -> None:
        """Authenticate with Anker Cloud and start MQTT monitoring."""
        try:
            from api.api import AnkerSolixApi
        except ImportError:
            logger.error(
                "Failed to import anker-solix-api. Install with: "
                "pip install git+https://github.com/thomluther/anker-solix-api.git"
            )
            raise

        # Capture the event loop for thread-safe callback scheduling
        self._loop = asyncio.get_running_loop()
        # Create the event here (after the loop is running) to avoid deprecation
        self._force_trigger_event = asyncio.Event()

        # --- Step 1: Authenticate ---
        logger.info("Authenticating with Anker Cloud API...")

        import aiohttp

        # Create aiohttp session WITHOUT async with — we need it to persist
        # for the entire MQTT session lifetime (the API uses it internally)
        self._websession = aiohttp.ClientSession()

        # AnkerSolixApi creates its own AnkerSolixClientSession internally
        # when apisession is not provided.
        self._api = AnkerSolixApi(
            email=self._config.anker_email,
            password=self._config.anker_password,
            countryId=self._config.anker_country,
            websession=self._websession,
        )

        await self._api.async_authenticate()
        logger.info("Successfully authenticated with Anker Cloud API")

        # --- Step 2: Discover devices ---
        # Standalone F3800 PPS devices are NOT in any "site" — they appear
        # in get_bind_devices() instead. Try both methods to maximize coverage.
        logger.info("Discovering devices from Anker Cloud...")
        try:
            await self._api.update_sites()
        except Exception:
            logger.debug("update_sites() failed (non-critical for standalone PPS)")

        if not self._api.devices:
            # F3800 as a standalone device won't be in any site.
            # get_bind_devices() lists all BLE/WiFi-bound devices.
            logger.info("No devices in sites — trying get_bind_devices() for standalone PPS...")
            try:
                await self._api.get_bind_devices()
            except Exception:
                logger.warning("get_bind_devices() also failed")

        logger.info(
            "Found %d device(s) in account", len(self._api.devices) if self._api.devices else 0
        )

        # Find our F3800 by SN
        target_sn = self._config.device_sn
        devices = self._api.devices or {}

        if target_sn and target_sn in devices:
            dev = devices[target_sn]
            self._device_info = {
                "sn": dev.get("device_sn", target_sn),
                "pn": dev.get("device_pn", ""),
                "model": dev.get("device_pn_name", "F3800"),
                "sw_version": dev.get("sw_version", ""),
                "mqtt_supported": dev.get("mqtt_supported", False),
            }
            logger.info("Found F3800: SN=%s, model=%s", target_sn, dev.get("device_pn_name", ""))
        elif devices:
            available = list(devices.keys())
            if target_sn:
                logger.warning(
                    "F3800 with SN=%s not found. Available SNs: %s",
                    target_sn,
                    available,
                )
            else:
                # No SN specified — try to auto-detect an F3800
                for sn, dev in devices.items():
                    pn = dev.get("device_pn", "")
                    name = dev.get("device_pn_name", "")
                    if "A1790" in pn or "F3800" in name:
                        target_sn = sn
                        self._device_info = {
                            "sn": sn,
                            "pn": pn,
                            "model": name,
                            "sw_version": dev.get("sw_version", ""),
                            "mqtt_supported": dev.get("mqtt_supported", False),
                        }
                        logger.info("Auto-detected F3800: SN=%s, model=%s", sn, name)
                        break
                if not target_sn:
                    logger.warning(
                        "No F3800 auto-detected. Available devices: %s",
                        [(sn, dev.get("device_pn_name", "")) for sn, dev in devices.items()],
                    )
        else:
            logger.warning("No devices found in Anker account")

        # --- Step 3: Start MQTT session ---
        logger.info("Starting MQTT session...")
        self._mqtt_session = await self._api.startMqttSession()

        if not self._mqtt_session:
            raise ConnectionError(
                "Failed to start MQTT session. Check your credentials and internet connection."
            )

        if not self._mqtt_session.is_connected():
            raise ConnectionError("MQTT broker connection failed")

        logger.info("Connected to Anker MQTT broker")

        # --- Step 4: Register update callback ---
        # MqttUpdateCallback signature: Callable[[str], None]
        # Called with device_sn whenever new decoded data is available.
        # Invoked from paho-mqtt's thread — must use run_coroutine_threadsafe.
        self._api.mqtt_update_callback(func=self._on_mqtt_update)
        logger.info("Registered MQTT update callback")

        # --- Step 5: Subscribe to topics and start poller ---
        # get_topic_prefix needs the full device dict (must include device_pn)
        # to construct the topic path: dt/{app_name}/{device_pn}/{device_sn}/
        device_dict = devices.get(target_sn) if target_sn else None
        topics: set[str] = set()

        # Get topic prefixes for subscription
        if device_dict and (prefix := self._mqtt_session.get_topic_prefix(deviceDict=device_dict)):
            topics.add(f"{prefix}#")
            logger.info("Subscribing to data topic: %s#", prefix)

        if device_dict and (
            cmd_prefix := self._mqtt_session.get_topic_prefix(
                deviceDict=device_dict, publish=True
            )
        ):
            topics.add(f"{cmd_prefix}#")
            logger.info("Subscribing to command topic: %s#", cmd_prefix)

        if not topics:
            raise ConnectionError(
                "No MQTT topics to subscribe to — is DEVICE_SN correct?"
            )

        # trigger_devices is required by message_poller — tells it which devices
        # to send realtime triggers to during the poll loop.
        trigger_devices = {target_sn} if target_sn else set()

        self._running = True
        self._poller_task = asyncio.create_task(
            self._mqtt_session.message_poller(
                topics=topics,
                trigger_devices=trigger_devices,
                msg_callback=None,  # Library uses internal mqtt_received handler
                timeout=120,
            )
        )

        # --- Step 6: Save device dict and start periodic trigger loop ---
        self._device_dict = device_dict or {}

        # Send initial realtime trigger
        self._send_trigger("initial")

        # Periodic trigger loop: every poll_interval seconds, send a
        # realtime_trigger to request fresh data from the F3800.
        # The force_trigger_event allows the menu to request an immediate poll.
        logger.info(
            "MQTT monitoring active. Polling every %ds (use menu to change).",
            self._config.poll_interval,
        )

        try:
            while self._running:
                # Wait for the poll interval, but break early if forced
                try:
                    await asyncio.wait_for(
                        self._force_trigger_event.wait(),
                        timeout=self._config.poll_interval,
                    )
                    self._force_trigger_event.clear()
                    reason = "forced"
                except asyncio.TimeoutError:
                    reason = "scheduled"

                if not self._running:
                    break

                # Check connection health
                if self._mqtt_session and not self._mqtt_session.is_connected():
                    logger.warning("MQTT connection lost, attempting reconnect...")
                    break

                # Send realtime trigger for fresh data
                self._send_trigger(reason)

        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Stop MQTT monitoring and disconnect."""
        self._running = False

        # Cancel any pending debounce timer
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None

        if self._poller_task and not self._poller_task.done():
            self._poller_task.cancel()
            try:
                await self._poller_task
            except asyncio.CancelledError:
                pass

        # stopMqttSession is synchronous (not awaitable)
        if self._api:
            try:
                self._api.stopMqttSession()
                logger.info("MQTT session disconnected via API")
            except Exception:
                logger.warning("Error disconnecting MQTT session via API")
                # Fallback: try direct session cleanup
                if self._mqtt_session:
                    try:
                        self._mqtt_session.cleanup()
                    except Exception:
                        pass

        if self._websession:
            try:
                await self._websession.close()
                logger.info("aiohttp session closed")
            except Exception:
                logger.warning("Error closing aiohttp session")

        self._mqtt_session = None
        self._api = None
        self._websession = None

    def force_trigger(self) -> None:
        """Request an immediate data poll from the F3800.

        Safe to call from the asyncio event loop thread.
        """
        if self._force_trigger_event is not None:
            self._force_trigger_event.set()

    def set_poll_interval(self, seconds: int) -> None:
        """Change the polling interval in seconds.

        Wakes the trigger loop so the new interval takes effect immediately
        (otherwise the current wait would continue with the old timeout).
        """
        if seconds < 10:
            logger.warning("Minimum poll interval is 10s, clamping")
            seconds = 10
        self._config.poll_interval = seconds
        logger.info("Poll interval changed to %ds", seconds)
        # Wake the trigger loop so it re-enters with the new interval
        if self._force_trigger_event is not None:
            self._force_trigger_event.set()

    def _send_trigger(self, reason: str = "scheduled") -> None:
        """Send a realtime trigger to the F3800 to request current data."""
        self._poll_count += 1
        self._last_trigger_time = time.monotonic()

        try:
            if self._device_dict and self._mqtt_session:
                self._mqtt_session.realtime_trigger(deviceDict=self._device_dict)
                logger.info(
                    "Trigger #%d sent (%s), next poll in %ds",
                    self._poll_count,
                    reason,
                    self._config.poll_interval,
                )
            else:
                logger.warning("Cannot send trigger: no device_dict or mqtt_session")
        except Exception:
            logger.warning("Error sending realtime trigger: %s", reason)

    async def get_device_info(self) -> dict:
        """Return cached device information."""
        return self._device_info.copy()

    def _on_mqtt_update(self, device_sn: str) -> None:
        """Callback from anker-solix-api when new MQTT data is decoded.

        This is invoked from paho-mqtt's background thread, NOT the asyncio
        event loop thread.

        The F3800 sends its telemetry across MULTIPLE MQTT messages:
          - state_info (battery_soc, power readings, etc.)
          - param_info (firmware, expansion packs, switches, etc.)
        The callback fires on EACH message, but data is only useful once
        enough messages have accumulated. We debounce: cancel any pending
        processing and schedule a new one after a short delay, allowing
        more MQTT messages to arrive and populate mqtt_data.

        Args:
            device_sn: The serial number of the updated device.
        """
        target_sn = self._config.device_sn

        # Only process updates for our target device
        if target_sn and device_sn != target_sn:
            return

        # Quick check: how many sensor keys do we have so far?
        mqtt_data = self._read_mqtt_data(device_sn)
        sensor_key_count = len(mqtt_data) if mqtt_data else 0

        # Cancel any pending debounce timer and schedule a new one.
        # This ensures we always wait _DEBOUNCE_SECONDS after the LATEST
        # message before processing, giving time for all messages to arrive.
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None

            # If we already have enough data, use a shorter delay
            delay = 0.5 if sensor_key_count >= _MIN_SENSOR_KEYS else _DEBOUNCE_SECONDS

            self._debounce_timer = threading.Timer(
                delay,
                self._process_accumulated_data,
                args=[device_sn],
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

        logger.debug(
            "MQTT update for %s: %d keys so far, processing in %.1fs",
            device_sn,
            sensor_key_count,
            delay,
        )

    def _read_mqtt_data(self, device_sn: str) -> dict:
        """Read accumulated mqtt_data for a device.

        Prefers mqtt_session.mqtt_data (richer, native types) with
        fallback to api.devices[sn]["mqtt_data"] (string values, subset).

        Returns a shallow copy so the caller has a stable snapshot
        (paho's background thread may be updating the original dict
        concurrently via the | merge operator).
        """
        if self._mqtt_session and hasattr(self._mqtt_session, "mqtt_data"):
            data = self._mqtt_session.mqtt_data.get(device_sn, {})
            if data:
                return dict(data)
        if self._api:
            device = self._api.devices.get(device_sn, {})
            data = device.get("mqtt_data", {})
            if data:
                return dict(data)
        return {}

    def _process_accumulated_data(self, device_sn: str) -> None:
        """Process accumulated MQTT data after debounce delay.

        Called from the debounce Timer thread (still NOT the asyncio loop).
        Reads the now-fully-accumulated mqtt_data and schedules the
        async notification on the event loop.
        """
        # Read accumulated data (reuses _read_mqtt_data with fallback)
        mqtt_data = self._read_mqtt_data(device_sn)

        if not mqtt_data:
            if self._running:
                logger.warning("Empty mqtt_data for device %s after debounce (session may have been cleaned up)", device_sn)
            return

        # Filter out metadata keys (and their '?' suffix variants)
        # to count actual sensor values
        meta_prefixes = ("last_message", "last_update", "msg_timestamp", "topics",
                         "set_realtime_trigger", "trigger_timeout_sec")
        sensor_keys = [k for k in mqtt_data
                       if not any(k == m or k == m + "?" for m in meta_prefixes)]

        if len(sensor_keys) < _MIN_SENSOR_KEYS:
            logger.debug(
                "Insufficient sensor data for %s: %d keys (need %d), skipping",
                device_sn,
                len(sensor_keys),
                _MIN_SENSOR_KEYS,
            )
            return

        try:
            f3800_data = F3800Data.from_mqtt_data(mqtt_data, device_sn=device_sn)

            # Schedule the async notification on the event loop
            if self._loop and not self._loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._notify(f3800_data), self._loop
                )
            else:
                logger.warning("Event loop unavailable, dropping data update")

        except Exception:
            logger.exception("Error mapping MQTT data to F3800Data")
