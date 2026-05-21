"""Data models for Anker Solix F3800 telemetry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ExpansionPackData:
    """Telemetry for a single F3800 expansion battery pack."""

    sn: str = ""
    pack_type: str = ""
    soc: int | None = None  # State of charge (%)
    soh: int | None = None  # State of health (%)
    temperature: float | None = None  # Pack temperature (°C)


@dataclass
class F3800Data:
    """Standardized data payload from the Anker Solix F3800.

    This is the unified data model regardless of whether data comes from
    the Cloud MQTT API or the local Modbus TCP connection.
    """

    # Metadata
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "unknown"  # "mqtt" or "modbus"
    device_sn: str = ""

    # Battery — main unit
    battery_soc: int | None = None  # Aggregate SoC including expansion packs (%)
    main_battery_soc: int | None = None  # Main unit state of charge (%)
    charging_status: int | None = None  # Charging status code (0=idle, 1=charging)
    temperature: float | None = None  # Main unit temperature (°C)
    max_soc: int | None = None  # Max SoC limit (%)

    # Input power (charging)
    ac_input_power: int | None = None  # AC input power (W)
    photovoltaic_power: int | None = None  # Total solar/DC input power (W)
    pv_1_power: int | None = None  # Solar PV input 1 power (W)
    pv_2_power: int | None = None  # Solar PV input 2 power (W)
    bat_charge_power: int | None = None  # Battery charge power (W)
    bat_discharge_power: int | None = None  # Battery discharge power (W)

    # Output power (discharging)
    output_power_total: int | None = None  # Total output power (W)
    ac_output_power: int | None = None  # AC output power (W)
    ac_output_power_switch: bool | None = None  # AC output on/off
    dc_output_power_switch: bool | None = None  # DC output on/off
    remaining_time_hours: float | None = None  # Estimated time remaining (hours)

    # USB ports
    usbc_1_power: int | None = None  # USB-C port 1 power (W)
    usbc_2_power: int | None = None  # USB-C port 2 power (W)
    usbc_3_power: int | None = None  # USB-C port 3 power (W)
    usba_1_power: int | None = None  # USB-A port 1 power (W)
    usba_2_power: int | None = None  # USB-A port 2 power (W)

    # DC 12V
    dc_12v_1_power: int | None = None  # DC 12V output power (W)

    # Settings (may not be available on every update)
    ac_input_limit: int | None = None  # AC input limit (W)
    ac_fast_charge_switch: bool | None = None  # Fast charge on/off

    # Expansion packs (up to 5 for F3800 + battery combo)
    expansion_packs: int | None = None  # Number of connected expansion packs
    expansions: list[ExpansionPackData] = field(default_factory=list)

    @property
    def total_input_power(self) -> int | None:
        """Calculate total input power (AC + solar)."""
        ac = self.ac_input_power or 0
        solar = self.photovoltaic_power or 0
        total = ac + solar
        return total if total > 0 else None

    @property
    def is_charging(self) -> bool | None:
        """Determine if the F3800 is currently charging."""
        if self.charging_status is not None:
            return self.charging_status > 0
        if self.bat_charge_power is not None:
            return self.bat_charge_power > 0
        if self.total_input_power is not None:
            return self.total_input_power > 0
        return None

    def to_dict(self) -> dict:
        """Convert to a flat dictionary suitable for CSV/DB logging."""
        d = {
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "device_sn": self.device_sn,
            "battery_soc": self.battery_soc,
            "main_battery_soc": self.main_battery_soc,
            "charging_status": self.charging_status,
            "temperature": self.temperature,
            "max_soc": self.max_soc,
            "ac_input_power": self.ac_input_power,
            "photovoltaic_power": self.photovoltaic_power,
            "pv_1_power": self.pv_1_power,
            "pv_2_power": self.pv_2_power,
            "bat_charge_power": self.bat_charge_power,
            "bat_discharge_power": self.bat_discharge_power,
            "total_input_power": self.total_input_power,
            "output_power_total": self.output_power_total,
            "ac_output_power": self.ac_output_power,
            "ac_output_power_switch": self.ac_output_power_switch,
            "dc_output_power_switch": self.dc_output_power_switch,
            "remaining_time_hours": self.remaining_time_hours,
            "usbc_1_power": self.usbc_1_power,
            "usbc_2_power": self.usbc_2_power,
            "usbc_3_power": self.usbc_3_power,
            "usba_1_power": self.usba_1_power,
            "usba_2_power": self.usba_2_power,
            "dc_12v_1_power": self.dc_12v_1_power,
            "ac_input_limit": self.ac_input_limit,
            "ac_fast_charge_switch": self.ac_fast_charge_switch,
            "expansion_packs": self.expansion_packs,
            "is_charging": self.is_charging,
        }
        # Flatten expansion pack data
        for i, exp in enumerate(self.expansions, 1):
            prefix = f"exp_{i}_"
            d[prefix + "sn"] = exp.sn
            d[prefix + "type"] = exp.pack_type
            d[prefix + "soc"] = exp.soc
            d[prefix + "soh"] = exp.soh
            d[prefix + "temperature"] = exp.temperature
        return d

    @classmethod
    def from_mqtt_data(cls, decoded: dict, device_sn: str = "") -> F3800Data:
        """Create an F3800Data instance from decoded MQTT data.

        The decoded dict comes from the anker-solix-api library's MQTT pipeline.
        Two sources with slightly different formats:
          - mqtt_session.mqtt_data[sn]: native int/float types, keys may have '?' suffix
          - api.devices[sn]["mqtt_data"]: numeric values stored as strings

        This method handles both formats by attempting both key variants
        and converting string values to numbers automatically.
        """

        def _get(key: str, default=None):
            """Get value from dict, trying both key and key+'?' variant.
            Converts string values to numeric types automatically.
            Handles api.devices format (string values like '777', '0.000')."""
            val = decoded.get(key)
            if val is None:
                # Try the '?' variant used by some library keys
                val = decoded.get(key + "?")
            if val is None:
                return default
            # Convert string values from api.devices mqtt_data
            if isinstance(val, str):
                try:
                    if "." in val:
                        # Float strings like '0.000' — round to int for
                        # power fields, keep float for time/temperature
                        fval = float(val)
                        if fval == int(fval):
                            return int(fval)
                        return fval
                    return int(val)
                except (ValueError, TypeError):
                    return val  # Return as-is (e.g., SN strings)
            return val

        def _to_bool(key: str, default=None) -> bool | None:
            val = _get(key)
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return val > 0
            return default

        def _get_exp(idx: int) -> ExpansionPackData:
            """Extract expansion pack data for the given 1-based index."""
            return ExpansionPackData(
                sn=_get(f"exp_{idx}_sn", ""),
                pack_type=_get(f"exp_{idx}_type", ""),
                soc=_get(f"exp_{idx}_soc"),
                soh=_get(f"exp_{idx}_soh"),
                temperature=_get(f"exp_{idx}_temperature"),
            )

        # Build expansion packs list (up to 5 for F3800)
        # NOTE: expansion_packs count may be 0 in api.devices but 5 in
        # mqtt_session.mqtt_data. Always check for actual pack data.
        expansions = []
        for i in range(1, 6):
            exp = _get_exp(i)
            if exp.sn:
                expansions.append(exp)

        return cls(
            timestamp=datetime.now(timezone.utc),
            source="mqtt",
            device_sn=device_sn,
            battery_soc=_get("battery_soc"),
            main_battery_soc=_get("main_battery_soc"),
            charging_status=_get("charging_status"),
            temperature=_get("temperature"),
            max_soc=_get("max_soc"),
            ac_input_power=_get("ac_input_power"),
            photovoltaic_power=_get("photovoltaic_power"),
            pv_1_power=_get("pv_1_power"),
            pv_2_power=_get("pv_2_power"),
            bat_charge_power=_get("bat_charge_power"),
            bat_discharge_power=_get("bat_discharge_power"),
            output_power_total=_get("output_power"),
            ac_output_power=_get("ac_output_power"),
            ac_output_power_switch=_to_bool("ac_output_power_switch"),
            dc_output_power_switch=_to_bool("dc_output_power_switch"),
            remaining_time_hours=_get("remaining_time_hours"),
            usbc_1_power=_get("usbc_1_power"),
            usbc_2_power=_get("usbc_2_power"),
            usbc_3_power=_get("usbc_3_power"),
            usba_1_power=_get("usba_1_power"),
            usba_2_power=_get("usba_2_power"),
            dc_12v_1_power=_get("dc_12v_output_power"),  # Not always present in MQTT data
            ac_input_limit=_get("ac_input_limit"),
            ac_fast_charge_switch=_to_bool("ac_fast_charge_switch"),
            expansion_packs=_get("expansion_packs"),
            expansions=expansions,
        )
