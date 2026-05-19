"""Unit tests for daily_summary.py — energy estimation and summary computation."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from daily_summary import (
    _estimate_wh,
    compute_daily_summary,
    write_summary_to_sqlite,
    SUMMARY_SCHEMA,
)


# Timezone for testing (PDT = UTC-7)
PDT = timezone(timedelta(hours=-7))


def _insert_rows(db: sqlite3.Connection, rows: list[dict]) -> None:
    """Insert test rows into f3800_telemetry."""
    columns = [
        "timestamp", "battery_soc", "temperature",
        "ac_input_power", "ac_output_power",
        "photovoltaic_power", "pv_1_power", "pv_2_power",
        "bat_charge_power", "bat_discharge_power",
    ]
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)
    sql = f"INSERT INTO f3800_telemetry ({col_str}) VALUES ({placeholders})"
    for row in rows:
        values = tuple(row.get(c) for c in columns)
        db.execute(sql, values)
    db.commit()


def _create_test_db(rows: list[dict]) -> str:
    """Create a temporary SQLite DB with the f3800_telemetry table and insert rows."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = sqlite3.connect(tmp.name)
    db.execute("""
        CREATE TABLE f3800_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            battery_soc INTEGER,
            temperature REAL,
            ac_input_power INTEGER,
            ac_output_power INTEGER,
            photovoltaic_power INTEGER,
            pv_1_power INTEGER,
            pv_2_power INTEGER,
            bat_charge_power INTEGER,
            bat_discharge_power INTEGER
        )
    """)
    _insert_rows(db, rows)
    db.close()
    return tmp.name


class TestEstimateWh(unittest.TestCase):
    """Test the trapezoidal energy estimation."""

    def test_empty_rows(self):
        self.assertEqual(_estimate_wh([], 0), 0.0)

    def test_single_row(self):
        self.assertEqual(_estimate_wh([("2025-01-01T00:00:00+00:00", 100)], 1), 0.0)

    def test_constant_power(self):
        """100W for 1 hour = 100 Wh."""
        rows = [
            ("2025-01-01T00:00:00+00:00", 100),
            ("2025-01-01T01:00:00+00:00", 100),
        ]
        self.assertAlmostEqual(_estimate_wh(rows, 1), 100.0, places=1)

    def test_linear_ramp(self):
        """0W → 200W over 1 hour = 100 Wh (trapezoidal)."""
        rows = [
            ("2025-01-01T00:00:00+00:00", 0),
            ("2025-01-01T01:00:00+00:00", 200),
        ]
        self.assertAlmostEqual(_estimate_wh(rows, 1), 100.0, places=1)

    def test_five_minute_intervals(self):
        """100W at 5-min intervals for 30 min = 50 Wh."""
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(7):  # 0, 5, 10, 15, 20, 25, 30 min
            ts = (base + timedelta(minutes=i * 5)).isoformat()
            rows.append((ts, 100))
        # 6 intervals × (5/60 hr) × 100W = 50 Wh
        self.assertAlmostEqual(_estimate_wh(rows, 1), 50.0, places=1)

    def test_gap_skipped(self):
        """Gaps > 1 hour should be skipped (sleep mode)."""
        rows = [
            ("2025-01-01T00:00:00+00:00", 100),
            ("2025-01-01T02:00:00+00:00", 100),  # 2-hour gap — skipped
            ("2025-01-01T03:00:00+00:00", 100),  # 1-hour gap — valid
        ]
        # Only the last interval (1 hr × 100W = 100 Wh) counts
        self.assertAlmostEqual(_estimate_wh(rows, 1), 100.0, places=1)

    def test_none_power_treated_as_zero(self):
        """None power values should be treated as 0."""
        rows = [
            ("2025-01-01T00:00:00+00:00", 100),
            ("2025-01-01T01:00:00+00:00", None),
        ]
        # Average of 100 and 0 = 50, × 1 hr = 50 Wh
        self.assertAlmostEqual(_estimate_wh(rows, 1), 50.0, places=1)


class TestComputeDailySummary(unittest.TestCase):
    """Test compute_daily_summary with a real SQLite DB."""

    def test_no_data_for_date(self):
        """Empty database should return empty dict."""
        db_path = _create_test_db([])
        result = compute_daily_summary(db_path, "2025-04-17", PDT)
        self.assertEqual(result, {})
        Path(db_path).unlink()

    def test_basic_summary(self):
        """Compute summary from a few data points."""
        base = datetime(2025, 4, 17, 8, 0, tzinfo=PDT)
        rows = []
        for i in range(12):  # 8AM to 9AM, 5-min intervals
            ts = (base + timedelta(minutes=i * 5)).astimezone(timezone.utc)
            rows.append({
                "timestamp": ts.isoformat(),
                "battery_soc": 50 + i,
                "temperature": 20.0 + i * 0.5,
                "ac_input_power": 0,
                "ac_output_power": 100,
                "photovoltaic_power": 200 + i * 10,
                "pv_1_power": 100 + i * 5,
                "pv_2_power": 100 + i * 5,
                "bat_charge_power": 200,
                "bat_discharge_power": 0,
            })

        db_path = _create_test_db(rows)
        result = compute_daily_summary(db_path, "2025-04-17", PDT)

        self.assertEqual(result["date"], "2025-04-17")
        self.assertEqual(result["data_points"], 12)
        self.assertEqual(result["soc_start"], 50)
        self.assertEqual(result["soc_end"], 61)
        self.assertEqual(result["soc_min"], 50)
        self.assertEqual(result["soc_max"], 61)
        self.assertGreater(result["solar_wh"], 0)
        self.assertGreater(result["pv1_wh"], 0)
        self.assertGreater(result["pv2_wh"], 0)
        self.assertGreater(result["max_pv1_w"], 0)
        self.assertGreater(result["max_pv2_w"], 0)
        self.assertGreater(result["charge_wh"], 0)
        self.assertEqual(result["discharge_wh"], 0)
        self.assertEqual(result["ac_in_wh"], 0)

        Path(db_path).unlink()

    def test_max_pv_per_channel(self):
        """Max PV1/PV2 should capture the peak value independently."""
        base = datetime(2025, 4, 17, 10, 0, tzinfo=PDT)
        rows = [
            {"timestamp": (base + timedelta(minutes=0)).astimezone(timezone.utc).isoformat(),
             "battery_soc": 50, "temperature": 25, "ac_input_power": 0, "ac_output_power": 0,
             "photovoltaic_power": 300, "pv_1_power": 100, "pv_2_power": 200,
             "bat_charge_power": 300, "bat_discharge_power": 0},
            {"timestamp": (base + timedelta(minutes=5)).astimezone(timezone.utc).isoformat(),
             "battery_soc": 55, "temperature": 26, "ac_input_power": 0, "ac_output_power": 0,
             "photovoltaic_power": 800, "pv_1_power": 350, "pv_2_power": 450,
             "bat_charge_power": 800, "bat_discharge_power": 0},
            {"timestamp": (base + timedelta(minutes=10)).astimezone(timezone.utc).isoformat(),
             "battery_soc": 60, "temperature": 27, "ac_input_power": 0, "ac_output_power": 0,
             "photovoltaic_power": 400, "pv_1_power": 150, "pv_2_power": 250,
             "bat_charge_power": 400, "bat_discharge_power": 0},
        ]

        db_path = _create_test_db(rows)
        result = compute_daily_summary(db_path, "2025-04-17", PDT)

        self.assertEqual(result["max_pv1_w"], 350)
        self.assertEqual(result["max_pv2_w"], 450)
        self.assertEqual(result["max_pv_total_w"], 800)

        Path(db_path).unlink()


class TestWriteSummaryToSqlite(unittest.TestCase):
    """Test writing summaries to the daily_summary table."""

    def test_creates_table(self):
        """write_summary_to_sqlite should create the table if it doesn't exist."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()

        summary = {
            "date": "2025-04-17",
            "data_points": 12,
            "first_ts": "2025-04-17T08:00:00+00:00",
            "last_ts": "2025-04-17T09:00:00+00:00",
            "soc_start": 50, "soc_end": 61,
            "soc_min": 50, "soc_max": 61,
            "temp_min_c": 20.0, "temp_max_c": 25.0, "temp_avg_c": 22.5,
            "solar_wh": 500.0, "pv1_wh": 250.0, "pv2_wh": 250.0,
            "max_pv1_w": 350, "max_pv2_w": 450, "max_pv_total_w": 800,
            "peak_solar_time": "10:05",
            "charge_wh": 500.0, "discharge_wh": 0.0,
            "ac_in_wh": 0.0, "ac_out_wh": 100.0,
        }

        write_summary_to_sqlite(tmp.name, summary)

        db = sqlite3.connect(tmp.name)
        c = db.execute("SELECT * FROM daily_summary WHERE date = '2025-04-17'")
        row = c.fetchone()
        db.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "2025-04-17")  # date
        self.assertEqual(row[1], 12)  # data_points
        self.assertAlmostEqual(row[11], 500.0, places=1)  # solar_wh

        Path(tmp.name).unlink()

    def test_upsert_replaces(self):
        """Running twice for the same date should replace, not duplicate."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()

        summary1 = {
            "date": "2025-04-17", "data_points": 10,
            "first_ts": "", "last_ts": "",
            "soc_start": 50, "soc_end": 60,
            "soc_min": 50, "soc_max": 60,
            "temp_min_c": 20.0, "temp_max_c": 25.0, "temp_avg_c": 22.5,
            "solar_wh": 400.0, "pv1_wh": 200.0, "pv2_wh": 200.0,
            "max_pv1_w": 300, "max_pv2_w": 400, "max_pv_total_w": 700,
            "peak_solar_time": "11:00",
            "charge_wh": 400.0, "discharge_wh": 0.0,
            "ac_in_wh": 0.0, "ac_out_wh": 80.0,
        }

        summary2 = {**summary1, "data_points": 12, "solar_wh": 500.0}

        write_summary_to_sqlite(tmp.name, summary1)
        write_summary_to_sqlite(tmp.name, summary2)

        db = sqlite3.connect(tmp.name)
        c = db.execute("SELECT COUNT(*) FROM daily_summary WHERE date = '2025-04-17'")
        count = c.fetchone()[0]
        c2 = db.execute("SELECT data_points, solar_wh FROM daily_summary WHERE date = '2025-04-17'")
        row = c2.fetchone()
        db.close()

        self.assertEqual(count, 1)
        self.assertEqual(row[0], 12)
        self.assertAlmostEqual(row[1], 500.0, places=1)

        Path(tmp.name).unlink()


if __name__ == "__main__":
    unittest.main()
