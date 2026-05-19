"""Local Modbus TCP monitor client for Anker Solix F3800.

NOTE: This is a STUB implementation. The F3800 does not currently expose
Modbus TCP (port 502 is closed). To enable it:
1. Open the Anker Solix iOS app
2. Navigate to your F3800 device settings
3. Look for "Modbus TCP" toggle and enable it
4. Note the device IP address

Once Modbus TCP is enabled, this client can connect locally without any
cloud dependency or account credentials.

The register map for F3800 is not yet available in the official HA integration.
The Solarbank register map (addresses 10001-10265) is used as a starting reference
and will need to be validated/adjusted for the F3800.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.clients.base import BaseMonitor
from src.config import AppSettings
from src.models import F3800Data

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------
# F3800 Modbus Register Map (REFERENCE — based on Solarbank)
# These addresses are UNVERIFIED for F3800 and may need adjustment.
# Register 0x8000 (32768) can be read to get the product code,
# which is how the official HA integration identifies the device.
# ---------------------------------------------------------------
F3800_REGISTER_MAP: dict[str, dict[str, Any]] = {
    # Device identification
    "device_model": {"address": 32768, "type": "STRING", "count": 16},
    "device_sn": {"address": 10100, "type": "STRING", "count": 6},
    "device_sw_version": {"address": 10112, "type": "STRING", "count": 6},
    # Battery & power status
    "battery_status": {"address": 10001, "type": "UINT16"},
    "pv_power": {"address": 10002, "type": "INT32"},
    "battery_charging_power": {"address": 10008, "type": "INT32"},
    "load_power": {"address": 10010, "type": "INT32"},
    "battery_soc": {"address": 10014, "type": "UINT16"},
    "pv_total_generation": {"address": 10018, "type": "UINT32"},
    "ac_grid_output_power": {"address": 10208, "type": "INT32"},
    "rated_energy": {"address": 10250, "type": "UINT32"},
    "cumulative_charge_energy": {"address": 10262, "type": "UINT32"},
    "cumulative_discharge_energy": {"address": 10264, "type": "UINT32"},
}


class LocalModbusMonitor(BaseMonitor):
    """Monitor F3800 via local Modbus TCP connection.

    Connects directly to the F3800 on the local network using Modbus TCP.
    No cloud or internet access required. Requires Modbus TCP to be enabled
    in the Anker app settings.
    """

    def __init__(self, config: AppSettings) -> None:
        super().__init__(config)
        self._client: Any = None
        self._device_info: dict = {}
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Connect to F3800 via Modbus TCP and start polling."""
        try:
            from pymodbus.client import AsyncModbusTcpClient
        except ImportError:
            logger.error(
                "Failed to import pymodbus. Install with: pip install pymodbus"
            )
            raise

        host = self._config.f3800_ip
        port = 502  # Standard Modbus TCP port

        logger.info(f"Connecting to F3800 via Modbus TCP at {host}:{port}...")

        self._client = AsyncModbusTcpClient(host=host, port=port, timeout=10)

        connected = await self._client.connect()
        if not connected:
            logger.error(
                f"Failed to connect to F3800 at {host}:{port}. "
                f"Is Modbus TCP enabled in the Anker app?"
            )
            raise ConnectionError(
                f"Cannot connect to F3800 at {host}:{port}. "
                f"Enable Modbus TCP in the Anker app settings first."
            )

        logger.info(f"Connected to F3800 via Modbus TCP at {host}:{port}")

        # Read device identification
        try:
            product_code = await self._read_string(32768, 16)
            if product_code:
                logger.info(f"F3800 product code: {product_code}")
                self._device_info["product_code"] = product_code
        except Exception:
            logger.warning("Could not read product code from register 32768")

        try:
            serial_number = await self._read_string(10100, 6)
            if serial_number:
                logger.info(f"F3800 serial number: {serial_number}")
                self._device_info["serial_number"] = serial_number
                self._device_info["sn"] = serial_number
        except Exception:
            logger.warning("Could not read serial number from register 10100")

        # Start polling loop
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"Modbus polling active. Interval: {self._config.poll_interval}s"
        )

    async def stop(self) -> None:
        """Stop Modbus polling and disconnect."""
        self._running = False

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._client:
            self._client.close()
            logger.info("Modbus TCP disconnected")

        self._client = None

    async def get_device_info(self) -> dict:
        """Return cached device information."""
        return self._device_info.copy()

    async def _poll_loop(self) -> None:
        """Periodically poll F3800 registers and emit data updates."""
        while self._running:
            try:
                data = await self._read_all_registers()
                if data:
                    await self._notify(data)
            except Exception:
                logger.exception("Error during Modbus poll")

            await asyncio.sleep(self._config.poll_interval)

    async def _read_all_registers(self) -> F3800Data | None:
        """Read all known registers and return an F3800Data instance."""
        if not self._client or not self._client.connected:
            logger.warning("Modbus client not connected, skipping poll")
            return None

        from datetime import datetime, timezone

        # Read battery SOC
        battery_soc = await self._read_uint16(10014)

        # Read power values
        pv_power = await self._read_int32(10002)
        battery_charging_power = await self._read_int32(10008)
        load_power = await self._read_int32(10010)

        # Read cumulative energy
        cumulative_charge = await self._read_uint32(10262)
        cumulative_discharge = await self._read_uint32(10264)

        # Only return data if we got at least one valid reading
        if battery_soc is None and pv_power is None and load_power is None:
            return None

        return F3800Data(
            timestamp=datetime.now(timezone.utc),
            source="modbus",
            device_sn=self._device_info.get("sn", ""),
            battery_soc=battery_soc,
            ac_input_power=battery_charging_power,  # Charging power from AC/DC
            dc_input_power_total=pv_power,  # Solar input
            output_power_total=load_power,
            ac_output_power=None,  # Separate register not yet mapped
        )

    async def _read_uint16(self, address: int) -> int | None:
        """Read a UINT16 holding/input register."""
        try:
            result = await self._client.read_input_registers(address, 1)
            if not result.isError():
                return result.registers[0]
        except Exception:
            logger.debug(f"Error reading UINT16 at address {address}")
        return None

    async def _read_int32(self, address: int) -> int | None:
        """Read an INT32 (2 registers, big-endian)."""
        try:
            result = await self._client.read_input_registers(address, 2)
            if not result.isError():
                # Big-endian: first register is high word
                high = result.registers[0]
                low = result.registers[1]
                value = (high << 16) | low
                # Convert to signed int32
                if value >= 0x80000000:
                    value -= 0x100000000
                return value
        except Exception:
            logger.debug(f"Error reading INT32 at address {address}")
        return None

    async def _read_uint32(self, address: int) -> int | None:
        """Read a UINT32 (2 registers, big-endian)."""
        try:
            result = await self._client.read_input_registers(address, 2)
            if not result.isError():
                high = result.registers[0]
                low = result.registers[1]
                return (high << 16) | low
        except Exception:
            logger.debug(f"Error reading UINT32 at address {address}")
        return None

    async def _read_string(self, address: int, count: int) -> str | None:
        """Read a STRING from consecutive registers."""
        try:
            result = await self._client.read_input_registers(address, count)
            if not result.isError():
                # Each register holds 2 chars (bytes)
                chars = []
                for reg in result.registers:
                    chars.append(chr((reg >> 8) & 0xFF))
                    chars.append(chr(reg & 0xFF))
                return "".join(chars).rstrip("\x00").strip()
        except Exception:
            logger.debug(f"Error reading STRING at address {address}")
        return None
