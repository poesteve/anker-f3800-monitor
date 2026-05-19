"""Configuration management for Anker Solix F3800 Monitor."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class AppSettings:
    """Application settings loaded from environment variables."""

    # Anker Cloud credentials
    anker_email: str = ""
    anker_password: str = ""
    anker_country: str = "US"

    # F3800 device info
    device_sn: str = ""
    f3800_ip: str = "10.0.0.52"

    # Connection mode: "mqtt" or "modbus"
    connection_mode: str = "mqtt"

    # Data logging
    log_dir: str = "./data"
    log_to_csv: bool = True
    log_to_sqlite: bool = True

    # Polling interval (seconds) — how often to request fresh data from the F3800
    # Default: 600 (10 minutes). Also used by Modbus mode.
    poll_interval: int = 600

    # Database write interval (seconds) — minimum time between SQLite/CSV writes.
    # The display updates on every MQTT event, but the database only records
    # a snapshot at this interval. Default: 300 (5 minutes = 12 data points/hour).
    db_write_interval: int = 300

    # Database sleep timeout (seconds) — if PV1, PV2, and AC output are all
    # zero for this long, stop writing to the database ("sleep mode").
    # The first data point with activity resumes writes. Default: 1800 (30 min).
    db_sleep_timeout: int = 1800

    # Temperature unit: "F" for Fahrenheit, "C" for Celsius
    temp_unit: str = "F"

    # Timezone offset from UTC (hours). PDT = -7, PST = -8
    tz_offset: int = -7

    # ── ntfy.sh push notification alerts ──
    # ntfy topic name (acts as a channel — pick something unique/hard-to-guess)
    ntfy_topic: str = ""
    # SoC threshold (%) — alert when battery reaches this level
    alert_soc_threshold: int = 90
    # Cooldown between repeated alerts (seconds). Default: 3600 (1 hour)
    alert_cooldown: int = 3600
    # Solar-done alert time (HH:MM, 24-hour local time).
    # When PV1+PV2 are both 0 AND it's at/after this time, send a
    # "solar day is over" notification. Default: 19:30 (7:30PM).
    alert_solar_end_time: str = "19:30"

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> AppSettings:
        """Load settings from a .env file and environment variables."""
        if env_path is not None:
            load_dotenv(env_path)
        else:
            # Load ALL found .env files, from innermost to outermost.
            # Use override=True so the outer (project root) .env can fill in
            # credentials that the inner .env left empty.
            for candidate in (
                Path(__file__).parent.parent / ".env",   # anker_f3800_monitor/.env
                Path(__file__).parent.parent.parent / ".env",  # project root .env
                ".env",                                    # cwd .env
            ):
                if Path(candidate).exists():
                    load_dotenv(candidate, override=True)

        return cls(
            anker_email=os.getenv("ANKER_EMAIL", ""),
            anker_password=os.getenv("ANKER_PASSWORD", ""),
            anker_country=os.getenv("ANKER_COUNTRY", "US"),
            device_sn=os.getenv("DEVICE_SN", ""),
            f3800_ip=os.getenv("F3800_IP", "10.0.0.52"),
            connection_mode=os.getenv("CONNECTION_MODE", "mqtt").lower(),
            log_dir=os.getenv("LOG_DIR", "./data"),
            log_to_csv=os.getenv("LOG_TO_CSV", "true").lower() == "true",
            log_to_sqlite=os.getenv("LOG_TO_SQLITE", "true").lower() == "true",
            poll_interval=int(os.getenv("POLL_INTERVAL", "600")),
            db_write_interval=int(os.getenv("DB_WRITE_INTERVAL", "300")),
            db_sleep_timeout=int(os.getenv("DB_SLEEP_TIMEOUT", "1800")),
            temp_unit=os.getenv("TEMP_UNIT", "F").upper(),
            tz_offset=int(os.getenv("TZ_OFFSET", "-7")),
            ntfy_topic=os.getenv("NTFY_TOPIC", ""),
            alert_soc_threshold=int(os.getenv("ALERT_SOC_THRESHOLD", "90")),
            alert_cooldown=int(os.getenv("ALERT_COOLDOWN", "3600")),
            alert_solar_end_time=os.getenv("ALERT_SOLAR_END_TIME", "19:30"),
        )

    def validate(self) -> list[str]:
        """Validate settings and return a list of error messages (empty if valid)."""
        errors: list[str] = []

        if self.connection_mode == "mqtt":
            if not self.anker_email:
                errors.append("ANKER_EMAIL is required for MQTT mode")
            if not self.anker_password:
                errors.append("ANKER_PASSWORD is required for MQTT mode")
        elif self.connection_mode == "modbus":
            if not self.f3800_ip:
                errors.append("F3800_IP is required for Modbus mode")
        else:
            errors.append(f"Unknown CONNECTION_MODE: {self.connection_mode!r}. Use 'mqtt' or 'modbus'.")

        if self.temp_unit not in ("F", "C"):
            errors.append(f"TEMP_UNIT must be 'F' or 'C', got {self.temp_unit!r}")

        if self.db_write_interval < 60:
            errors.append(f"DB_WRITE_INTERVAL must be at least 60 seconds, got {self.db_write_interval}")

        if self.db_sleep_timeout != 0 and self.db_sleep_timeout < 300:
            errors.append(f"DB_SLEEP_TIMEOUT must be 0 (disabled) or at least 300 seconds, got {self.db_sleep_timeout}")

        if not self.device_sn and self.connection_mode == "mqtt":
            errors.append("DEVICE_SN is required for MQTT mode (find it in Anker app → Device Settings → About)")

        return errors
