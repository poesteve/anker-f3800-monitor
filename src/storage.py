"""Data storage for Anker Solix F3800 telemetry.

Supports logging F3800Data to:
- CSV file (append mode, one row per data update)
- SQLite database (with proper schema and indexing)
"""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from src.models import F3800Data

logger = logging.getLogger(__name__)

# CSV column order
CSV_COLUMNS = [
    "timestamp",
    "source",
    "device_sn",
    "battery_soc",
    "main_battery_soc",
    "charging_status",
    "temperature",
    "ac_input_power",
    "photovoltaic_power",
    "pv_1_power",
    "pv_2_power",
    "bat_charge_power",
    "bat_discharge_power",
    "total_input_power",
    "output_power_total",
    "ac_output_power",
    "ac_output_power_switch",
    "dc_output_power_switch",
    "remaining_time_hours",
    "usbc_1_power",
    "usbc_2_power",
    "usbc_3_power",
    "usba_1_power",
    "usba_2_power",
    "dc_12v_1_power",
    "max_soc",
    "ac_input_limit",
    "ac_fast_charge_switch",
    "expansion_packs",
    "is_charging",
    # Expansion pack data (5 packs x 5 fields)
    "exp_1_sn", "exp_1_type", "exp_1_soc", "exp_1_soh", "exp_1_temperature",
    "exp_2_sn", "exp_2_type", "exp_2_soc", "exp_2_soh", "exp_2_temperature",
    "exp_3_sn", "exp_3_type", "exp_3_soc", "exp_3_soh", "exp_3_temperature",
    "exp_4_sn", "exp_4_type", "exp_4_soc", "exp_4_soh", "exp_4_temperature",
    "exp_5_sn", "exp_5_type", "exp_5_soc", "exp_5_soh", "exp_5_temperature",
]

# SQLite schema — must match CSV_COLUMNS (minus the auto-increment id)
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS f3800_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    device_sn TEXT NOT NULL,
    battery_soc INTEGER,
    main_battery_soc INTEGER,
    charging_status INTEGER,
    temperature REAL,
    ac_input_power INTEGER,
    photovoltaic_power INTEGER,
    pv_1_power INTEGER,
    pv_2_power INTEGER,
    bat_charge_power INTEGER,
    bat_discharge_power INTEGER,
    total_input_power INTEGER,
    output_power_total INTEGER,
    ac_output_power INTEGER,
    ac_output_power_switch INTEGER,
    dc_output_power_switch INTEGER,
    remaining_time_hours REAL,
    usbc_1_power INTEGER,
    usbc_2_power INTEGER,
    usbc_3_power INTEGER,
    usba_1_power INTEGER,
    usba_2_power INTEGER,
    dc_12v_1_power INTEGER,
    max_soc INTEGER,
    ac_input_limit INTEGER,
    ac_fast_charge_switch INTEGER,
    expansion_packs INTEGER,
    is_charging INTEGER,
    exp_1_sn TEXT, exp_1_type TEXT, exp_1_soc INTEGER, exp_1_soh INTEGER, exp_1_temperature REAL,
    exp_2_sn TEXT, exp_2_type TEXT, exp_2_soc INTEGER, exp_2_soh INTEGER, exp_2_temperature REAL,
    exp_3_sn TEXT, exp_3_type TEXT, exp_3_soc INTEGER, exp_3_soh INTEGER, exp_3_temperature REAL,
    exp_4_sn TEXT, exp_4_type TEXT, exp_4_soc INTEGER, exp_4_soh INTEGER, exp_4_temperature REAL,
    exp_5_sn TEXT, exp_5_type TEXT, exp_5_soc INTEGER, exp_5_soh INTEGER, exp_5_temperature REAL
);

CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp
    ON f3800_telemetry(timestamp);

CREATE INDEX IF NOT EXISTS idx_telemetry_device_sn
    ON f3800_telemetry(device_sn);
"""

# Generate INSERT from CSV_COLUMNS so placeholder count always matches
SQLITE_INSERT = (
    "INSERT INTO f3800_telemetry ("
    + ", ".join(CSV_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in CSV_COLUMNS)
    + ")"
)


class DataStorage:
    """Handles persisting F3800Data to CSV and SQLite.

    Supports throttled writes: the display updates on every MQTT event,
    but the database only records a snapshot at a configurable interval
    (default 5 minutes). This keeps the real-time dashboard responsive
    while avoiding excessive database rows.

    Sleep mode: if PV1, PV2, and AC output are all zero for longer than
    db_sleep_timeout (default 30 min), DB writes are paused. The first
    data point with activity resumes writes immediately.
    """

    def __init__(
        self,
        log_dir: str = "./data",
        log_to_csv: bool = True,
        log_to_sqlite: bool = True,
        db_write_interval: int = 300,
        db_sleep_timeout: int = 1800,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._log_to_csv = log_to_csv
        self._log_to_sqlite = log_to_sqlite
        self._db_write_interval = db_write_interval  # seconds between DB writes
        self._db_sleep_timeout = db_sleep_timeout    # 0 = disabled, else seconds of inactivity before sleep
        self._db: aiosqlite.Connection | None = None
        self._csv_path: Path | None = None
        self._csv_file = None
        self._csv_writer = None
        self._write_lock = asyncio.Lock()
        self._last_write_time: float = 0.0  # monotonic time of last DB/CSV write
        # Sleep mode tracking
        self._last_activity_time: float = 0.0  # monotonic time of last PV/AC activity
        self._sleeping: bool = False

    async def start(self) -> None:
        """Initialize storage backends."""
        # Ensure log directory exists
        self._log_dir.mkdir(parents=True, exist_ok=True)

        if self._log_to_csv:
            date_str = datetime.now().strftime("%Y%m%d")
            self._csv_path = self._log_dir / f"f3800_log_{date_str}.csv"
            # Write header if file is new
            is_new = not self._csv_path.exists()
            self._csv_file = open(self._csv_path, "a", newline="", encoding="utf-8")
            self._csv_writer = csv.DictWriter(
                self._csv_file,
                fieldnames=CSV_COLUMNS,
            )
            if is_new:
                self._csv_writer.writeheader()
            logger.info(f"CSV logging to: {self._csv_path}")

        if self._log_to_sqlite:
            db_path = self._log_dir / "f3800_log.db"
            self._db = await aiosqlite.connect(str(db_path))

            # Check if existing DB has the old schema (fewer columns) and migrate
            try:
                cursor = await self._db.execute("PRAGMA table_info(f3800_telemetry)")
                columns = [row[1] for row in await cursor.fetchall()]
                if columns and len(columns) < len(CSV_COLUMNS):
                    # Old schema detected -- drop and recreate
                    logger.warning(
                        "Old SQLite schema detected (%d cols, need %d). Recreating table.",
                        len(columns),
                        len(CSV_COLUMNS) + 1,  # +1 for the id column
                    )
                    await self._db.execute("DROP TABLE IF EXISTS f3800_telemetry")
            except Exception:
                logger.debug("No existing table to migrate")

            await self._db.executescript(SQLITE_SCHEMA)
            await self._db.commit()
            logger.info(f"SQLite logging to: {db_path}")

    async def stop(self) -> None:
        """Close storage backends."""
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None

        if self._db:
            await self._db.close()
            self._db = None

    def reset_write_timer(self) -> None:
        """Force the next write() call to bypass throttling.

        Used by the 'poll now' menu command to ensure the forced
        poll is immediately persisted to the database.
        """
        self._last_write_time = 0.0

    def wake(self) -> None:
        """Wake from sleep mode, allowing DB writes to resume.

        Used by the 'poll now' menu command to override sleep mode
        and force an immediate write. Also resets the idle timer so
        the system gets a full timeout period before re-entering sleep.
        """
        import time
        self._sleeping = False
        self._last_activity_time = time.monotonic()  # Restart idle timer

    @property
    def sleeping(self) -> bool:
        """Whether DB writes are currently paused due to inactivity."""
        return self._sleeping

    def _is_active(self, data: F3800Data) -> bool:
        """Check if there is meaningful activity (solar input or AC output).

        Returns True if any of PV1 power, PV2 power, or AC output power > 0.
        """
        pv1 = data.pv_1_power or 0
        pv2 = data.pv_2_power or 0
        ac_out = data.ac_output_power or 0
        return pv1 > 0 or pv2 > 0 or ac_out > 0

    def _update_sleep_state(self, data: F3800Data) -> None:
        """Update sleep mode state based on current data activity.

        - If sleep timeout is 0, sleep mode is disabled.
        - If activity is detected and we're sleeping, wake up immediately.
        - If activity is detected and we're awake, update the activity timer.
        - If no activity and the timeout has elapsed, enter sleep mode.
        - On cold start with no prior activity, we initialize the activity
          timer from the first data point (even idle ones) so sleep can
          activate after the timeout.
        """
        import time

        # Sleep mode disabled
        if self._db_sleep_timeout == 0:
            return

        if self._is_active(data):
            self._last_activity_time = time.monotonic()
            if self._sleeping:
                self._sleeping = False
                logger.info("DB sleep mode ended — activity detected (PV1=%s, PV2=%s, AC out=%s)",
                            data.pv_1_power, data.pv_2_power, data.ac_output_power)
        else:
            # No activity — start tracking idle time from the first data point
            if self._last_activity_time == 0.0:
                self._last_activity_time = time.monotonic()

            # Check if we should enter sleep mode
            if not self._sleeping:
                idle_seconds = time.monotonic() - self._last_activity_time
                if idle_seconds >= self._db_sleep_timeout:
                    self._sleeping = True
                    logger.info(
                        "DB sleep mode entered — no PV/AC activity for %d min",
                        idle_seconds // 60,
                    )

    def should_write(self, data: F3800Data | None = None) -> bool:
        """Check if a DB write should happen now.

        Combines three checks:
        1. Sleep mode — skip writes if PV1+PV2+AC output have been zero
           for longer than db_sleep_timeout.
        2. Throttle — minimum db_write_interval seconds between writes.
        3. First write always goes through.

        If data is provided, the sleep state is updated first.
        """
        if data is not None:
            self._update_sleep_state(data)

        if self._sleeping:
            return False

        import time
        now = time.monotonic()
        if self._last_write_time == 0.0:
            return True  # First write always goes through
        elapsed = now - self._last_write_time
        return elapsed >= self._db_write_interval

    async def write(self, data: F3800Data) -> None:
        """Write an F3800Data record to all enabled storage backends.

        Writes are throttled: a minimum of db_write_interval seconds must
        elapse between consecutive writes. Call reset_write_timer() before
        the next write to bypass throttling (e.g. for explicit "poll now"
        commands).

        Sleep mode: if PV1, PV2, and AC output have all been zero for
        longer than db_sleep_timeout (default 30 min), writes are paused.
        Activity on any of those channels immediately resumes writes.
        """
        if not self.should_write(data):
            return

        async with self._write_lock:
            import time
            self._last_write_time = time.monotonic()

            if self._log_to_csv:
                await self._write_csv(data)

            if self._log_to_sqlite:
                await self._write_sqlite(data)

    async def _write_csv(self, data: F3800Data) -> None:
        """Append a row to the CSV log file."""
        if not self._csv_writer:
            return

        row = data.to_dict()
        # Convert bools to int for CSV consistency
        for key, val in row.items():
            if isinstance(val, bool):
                row[key] = int(val)

        try:
            self._csv_writer.writerow(row)
            self._csv_file.flush()
        except Exception:
            logger.exception("Error writing to CSV")

    async def _write_sqlite(self, data: F3800Data) -> None:
        """Insert a record into the SQLite database."""
        if not self._db:
            return

        d = data.to_dict()
        # Convert bools to int for SQLite
        for key, val in d.items():
            if isinstance(val, bool):
                d[key] = int(val)

        # Build values in CSV_COLUMNS order -- this guarantees alignment
        # between INSERT columns and VALUES placeholders regardless of
        # dict ordering or missing keys.
        values = tuple(d.get(col) for col in CSV_COLUMNS)

        try:
            await self._db.execute(SQLITE_INSERT, values)
            await self._db.commit()
        except Exception:
            logger.exception("Error writing to SQLite")

    async def query_recent(
        self, device_sn: str = "", limit: int = 100
    ) -> list[dict]:
        """Query the most recent records from SQLite."""
        if not self._db:
            return []

        query = "SELECT * FROM f3800_telemetry"
        params: list[Any] = []

        if device_sn:
            query += " WHERE device_sn = ?"
            params.append(device_sn)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        try:
            cursor = await self._db.execute(query, params)
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]
        except Exception:
            logger.exception("Error querying SQLite")
            return []
