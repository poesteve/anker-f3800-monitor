"""Push notification alerts via ntfy.sh.

Sends push notifications for:
  - SoC threshold alerts (battery reaches configured %)
  - Solar-done alerts (both PV channels idle after configured evening time)

Includes cooldown logic and once-per-day tracking to avoid spam.

Setup:
  1. Install the ntfy app on your phone (iOS/Android)
  2. Subscribe to a topic (e.g. "anker-f3800-alerts")
  3. Set NTFY_TOPIC in your .env file
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError

from src.config import AppSettings
from src.models import F3800Data

logger = logging.getLogger(__name__)

# ntfy.sh public server URL
NTFY_BASE_URL = "https://ntfy.sh"


class Notifier:
    """Sends push notifications via ntfy.sh for SoC thresholds and solar-done alerts."""

    def __init__(self, config: AppSettings) -> None:
        self._topic = config.ntfy_topic
        self._threshold = config.alert_soc_threshold
        self._cooldown = config.alert_cooldown
        self._tz_offset = config.tz_offset
        self._last_alert_time: float = 0.0
        self._last_pv_time: float | None = None
        self._pending_solar_soc: int | None = None
        self._alert_active: bool = False  # True after threshold crossed, reset when SoC drops below
        self._solar_done_sent_date: str = ""  # YYYY-MM-DD of last solar-done alert (one per day)
        self._enabled = bool(config.ntfy_topic)

    @property
    def enabled(self) -> bool:
        """Whether notifications are configured and active."""
        return self._enabled

    async def check_soc(self, data: F3800Data) -> None:
        """Check SoC against threshold and send alert if needed.

        Alert logic:
        - When SoC >= threshold: send alert (once, with cooldown)
        - When SoC drops below threshold: reset so a new alert can fire
        - Cooldown prevents repeat alerts within the cooldown window
        """
        if not self._enabled:
            return

        soc = data.battery_soc
        if soc is None:
            return

        now = time.monotonic()

        if soc >= self._threshold:
            # SoC is at or above threshold
            if not self._alert_active:
                # First time crossing the threshold — send alert
                self._alert_active = True
                await asyncio.to_thread(self._send_soc_alert, soc)
                self._last_alert_time = now
                logger.info("SoC alert: %d%% >= %d%% — notification sent", soc, self._threshold)
            else:
                # Already above threshold — check cooldown for repeat alert
                elapsed = now - self._last_alert_time
                if elapsed >= self._cooldown:
                    await asyncio.to_thread(self._send_soc_alert, soc)
                    self._last_alert_time = now
                    logger.info("SoC reminder: %d%% (cooldown elapsed) — notification sent", soc)
        else:
            # SoC dropped below threshold — reset alert state
            if self._alert_active:
                logger.info("SoC alert cleared: %d%% < %d%%", soc, self._threshold)
            self._alert_active = False

    def _send_soc_alert(self, soc: int) -> None:
        """Send a SoC threshold alert via ntfy.sh."""
        title = "Anker F3800 Alert"
        message = f"Battery SoC at {soc}% (threshold: {self._threshold}%)"
        self._post_notification(title, message, priority="default", tags="battery")

    async def check_solar_done(self, data: F3800Data) -> None:
        """Check if solar day is over using 60-minute confirmation.

        Fires once per day when:
        - PV1 AND PV2 are both 0 (or None) — no solar input
        - 60 minutes have elapsed since the last PV-positive reading
        - We haven't already sent a solar-done alert today
        """
        if not self._enabled:
            return

        pv1 = data.pv_1_power or 0
        pv2 = data.pv_2_power or 0
        now = time.monotonic()

        if pv1 > 0 or pv2 > 0:
            self._last_pv_time = now
            self._pending_solar_soc = data.battery_soc
            return

        if self._last_pv_time is None:
            return

        elapsed = now - self._last_pv_time

        if elapsed < 3600:
            return

        # One alert per day
        utc_now = datetime.now(timezone.utc)
        local_now = utc_now + timedelta(hours=self._tz_offset)
        today_str = local_now.strftime("%Y-%m-%d")
        if self._solar_done_sent_date == today_str:
            return

        self._solar_done_sent_date = today_str
        soc = self._pending_solar_soc
        self._pending_solar_soc = None
        await asyncio.to_thread(self._send_solar_done_alert, data, soc)
        logger.info("Solar-done alert sent for %s (SoC at solar end: %s%%)", today_str, soc)

    def _send_solar_done_alert(self, data: F3800Data, soc: int | None = None) -> None:
        """Send a 'solar day is over' push notification with current stats."""
        title = "Anker F3800 Solar Done"
        soc = soc or data.battery_soc or "--"
        ac_out = data.ac_output_power or 0
        temp_c = data.temperature
        temp_f = round(temp_c * 9 / 5 + 32) if temp_c is not None else None

        lines = [f"SoC: {soc}%  |  AC Out: {ac_out}W"]
        if temp_c is not None and temp_f is not None:
            lines.append(f"Temp: {temp_c}C / {temp_f}F")
        lines.append("PV1 (Patio) + PV2 (Shed) = 0W")

        message = "\n".join(lines)
        self._post_notification(title, message, priority="default", tags="sunset")

    def _post_notification(self, title: str, message: str, *, priority: str = "default", tags: str = "") -> bool:
        """Send a push notification via ntfy.sh (shared helper).

        Returns True on success, False on failure.
        """
        url = f"{NTFY_BASE_URL}/{self._topic}"

        try:
            req = Request(url, data=message.encode("utf-8"), method="POST")
            req.add_header("Title", title)
            req.add_header("Priority", priority)
            if tags:
                req.add_header("Tags", tags)
            req.add_header("Content-Type", "text/plain; charset=utf-8")
            with urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    logger.debug("ntfy notification sent: %s", title)
                    return True
                else:
                    logger.warning("ntfy returned status %d for %s", resp.status, title)
                    return False
        except URLError as e:
            logger.warning("Failed to send ntfy notification '%s': %s", title, e)
            return False
        except Exception:
            logger.exception("Unexpected error sending ntfy notification '%s'", title)
            return False

    async def send_test(self) -> bool:
        """Send a test notification. Returns True on success."""
        if not self._enabled:
            return False

        return await asyncio.to_thread(
            self._post_notification,
            "Anker F3800 Test",
            "ntfy.sh notifications are working!",
            priority="low",
            tags="white_check_mark",
        )
