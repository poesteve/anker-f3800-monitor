"""Unit tests for DataStorage — throttled writes and sleep mode."""

import time
import unittest

from src.config import AppSettings
from src.models import F3800Data
from src.storage import DataStorage


def _make_data(pv1: int = 0, pv2: int = 0, ac_out: int = 0) -> F3800Data:
    """Create an F3800Data instance with specified PV/AC output values."""
    return F3800Data.from_mqtt_data(
        {
            "battery_soc": 50,
            "temperature": 25,
            "ac_output_power": ac_out,
            "ac_output_power_switch": 1 if ac_out else 0,
            "dc_output_power_switch": 0,
            "photovoltaic_power": pv1 + pv2,
            "output_power": ac_out,
            "pv_1_power": pv1,
            "pv_2_power": pv2,
        },
        device_sn="TESTSN",
    )


class TestConfigDBSettings(unittest.TestCase):
    """Test AppSettings defaults and validation for DB write/sleep settings."""

    def test_defaults(self):
        cfg = AppSettings()
        self.assertEqual(cfg.db_write_interval, 300)
        self.assertEqual(cfg.db_sleep_timeout, 1800)

    def test_write_interval_minimum(self):
        cfg = AppSettings(db_write_interval=30)
        errors = cfg.validate()
        self.assertTrue(any("DB_WRITE_INTERVAL" in e for e in errors))

    def test_write_interval_valid(self):
        cfg = AppSettings(db_write_interval=60)
        errors = cfg.validate()
        self.assertFalse(any("DB_WRITE_INTERVAL" in e for e in errors))

    def test_sleep_timeout_zero_allowed(self):
        cfg = AppSettings(db_sleep_timeout=0)
        errors = cfg.validate()
        self.assertFalse(any("DB_SLEEP_TIMEOUT" in e for e in errors))

    def test_sleep_timeout_below_minimum(self):
        cfg = AppSettings(db_sleep_timeout=60)
        errors = cfg.validate()
        self.assertTrue(any("DB_SLEEP_TIMEOUT" in e for e in errors))

    def test_sleep_timeout_valid(self):
        cfg = AppSettings(db_sleep_timeout=300)
        errors = cfg.validate()
        self.assertFalse(any("DB_SLEEP_TIMEOUT" in e for e in errors))


class TestStorageThrottling(unittest.TestCase):
    """Test DataStorage write throttling (db_write_interval)."""

    def test_first_write_always_allowed(self):
        s = DataStorage(db_write_interval=300)
        self.assertTrue(s.should_write(_make_data()))

    def test_write_blocked_within_interval(self):
        s = DataStorage(db_write_interval=300)
        # Simulate a write just happened
        s._last_write_time = time.monotonic()
        self.assertFalse(s.should_write(_make_data()))

    def test_write_allowed_after_interval(self):
        s = DataStorage(db_write_interval=300)
        # Simulate a write 301 seconds ago
        s._last_write_time = time.monotonic() - 301
        self.assertTrue(s.should_write(_make_data()))

    def test_reset_write_timer(self):
        s = DataStorage(db_write_interval=300)
        s._last_write_time = time.monotonic()
        s.reset_write_timer()
        self.assertTrue(s.should_write(_make_data()))


class TestStorageSleepMode(unittest.TestCase):
    """Test DataStorage sleep mode (db_sleep_timeout)."""

    def test_starts_awake(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        self.assertFalse(s.sleeping)

    def test_active_data_keeps_awake(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        data = _make_data(pv1=100, pv2=50, ac_out=200)
        s.should_write(data)
        self.assertFalse(s.sleeping)

    def test_enters_sleep_after_timeout(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        # Simulate activity 1900 seconds ago, then idle data
        s._last_activity_time = time.monotonic() - 1900
        idle = _make_data(pv1=0, pv2=0, ac_out=0)
        s.should_write(idle)
        self.assertTrue(s.sleeping)

    def test_sleep_blocks_write(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        s._sleeping = True
        self.assertFalse(s.should_write(_make_data()))

    def test_activity_wakes_from_sleep(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        s._sleeping = True
        active = _make_data(pv1=100, pv2=0, ac_out=0)
        s.should_write(active)
        self.assertFalse(s.sleeping)

    def test_pv2_wakes_from_sleep(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        s._sleeping = True
        active = _make_data(pv1=0, pv2=50, ac_out=0)
        s.should_write(active)
        self.assertFalse(s.sleeping)

    def test_ac_output_wakes_from_sleep(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        s._sleeping = True
        active = _make_data(pv1=0, pv2=0, ac_out=100)
        s.should_write(active)
        self.assertFalse(s.sleeping)

    def test_wake_method(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        s._sleeping = True
        s._last_activity_time = time.monotonic() - 5000
        s.wake()
        self.assertFalse(s.sleeping)
        # wake() should reset the idle timer
        self.assertGreater(s._last_activity_time, time.monotonic() - 2)

    def test_cold_start_initializes_activity_timer(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        # _last_activity_time starts at 0.0
        self.assertEqual(s._last_activity_time, 0.0)
        idle = _make_data(pv1=0, pv2=0, ac_out=0)
        s.should_write(idle)
        # Timer should be initialized (not 0.0) even with idle data
        self.assertGreater(s._last_activity_time, 0.0)
        # But not sleeping yet (timeout hasn't elapsed)
        self.assertFalse(s.sleeping)

    def test_sleep_disabled_with_zero_timeout(self):
        s = DataStorage(db_write_interval=300, db_sleep_timeout=0)
        # Simulate long idle
        s._last_activity_time = time.monotonic() - 99999
        idle = _make_data(pv1=0, pv2=0, ac_out=0)
        s.should_write(idle)
        self.assertFalse(s.sleeping)

    def test_combined_throttle_and_sleep(self):
        """Even when throttle allows, sleep should block writes."""
        s = DataStorage(db_write_interval=300, db_sleep_timeout=1800)
        # First write goes through
        self.assertTrue(s.should_write(_make_data()))
        # Now simulate: write just happened, and we're sleeping
        s._last_write_time = time.monotonic()
        s._sleeping = True
        # Both throttle AND sleep block the write
        self.assertFalse(s.should_write(_make_data()))
        # Wake up — but throttle still blocks
        s.wake()
        self.assertFalse(s.should_write(_make_data()))
        # After throttle interval passes, write is allowed again
        s._last_write_time = time.monotonic() - 301
        self.assertTrue(s.should_write(_make_data()))


if __name__ == "__main__":
    unittest.main()
