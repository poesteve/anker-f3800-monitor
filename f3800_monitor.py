#!/usr/bin/env python3
"""Anker Solix F3800 Monitor — Main entry point.

Connects to your Anker Solix F3800 power station and displays/logs
real-time telemetry data including battery SOC, charging/discharging power,
and individual port power readings.

Usage:
    1. Copy .env.example to .env and fill in your Anker credentials
    2. Run: python f3800_monitor.py

Connection modes:
    - mqtt:   Uses Anker Cloud API + MQTT (requires internet + Anker account)
    - modbus: Uses local Modbus TCP (requires enabling in Anker app, no cloud)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from src.config import AppSettings
from src.models import F3800Data
from src.display import ConsoleDisplay
from src.storage import DataStorage
from src.notifier import Notifier
from src.clients.base import BaseMonitor
# docs module imported lazily in _docs_menu() to avoid Rich dependency at startup

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Reduce noise from third-party libraries
    logging.getLogger("paho").setLevel(logging.WARNING)
    logging.getLogger("pymodbus").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def create_monitor(config: AppSettings) -> BaseMonitor:
    """Create the appropriate monitor based on connection mode."""
    if config.connection_mode == "mqtt":
        from src.clients.mqtt_cloud import CloudMqttMonitor
        return CloudMqttMonitor(config)
    elif config.connection_mode == "modbus":
        from src.clients.modbus_local import LocalModbusMonitor
        return LocalModbusMonitor(config)
    else:
        raise ValueError(f"Unknown connection mode: {config.connection_mode!r}")


async def run_monitor(config: AppSettings, verbose: bool = False, headless: bool = False) -> None:
    """Main application loop."""
    setup_logging(verbose)

    # Validate configuration
    errors = config.validate()
    if errors:
        print("❌ Configuration errors:")
        for err in errors:
            print(f"   - {err}")
        print("\n💡 Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    if not headless:
        # Print startup banner
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=config.tz_offset))
        now_local = datetime.now(tz)

        print("╔══════════════════════════════════════════════════════╗")
        print("║       Anker Solix F3800 Monitor  v0.2.0              ║")
        print("╠══════════════════════════════════════════════════════╣")
        mode_label = "Cloud MQTT" if config.connection_mode == "mqtt" else "Local Modbus TCP"
        print(f"║  Connection:  {mode_label:<38}║")
        print(f"║  Device SN:   {config.device_sn or '(auto-detect)':<38}║")
        if config.connection_mode == "modbus":
            print(f"║  F3800 IP:    {config.f3800_ip:<38}║")
        poll_min = config.poll_interval // 60
        poll_sec = config.poll_interval % 60
        poll_str = f"{poll_min}m {poll_sec}s" if poll_sec else f"{poll_min}m"
        print(f"║  Poll interval: {poll_str:<37}║")
        print(f"║  Local time:  {now_local.strftime('%H:%M %Z'):<38}║")
        print(f"║  CSV logging: {'ON' if config.log_to_csv else 'OFF':<38}║")
        print(f"║  SQLite log:  {'ON' if config.log_to_sqlite else 'OFF':<38}║")
        db_min = config.db_write_interval // 60
        db_pts = 60 // db_min if db_min >= 1 else "?"
        db_label = f"{db_min}m ({db_pts} pts/hr)"
        sleep_min = config.db_sleep_timeout // 60 if config.db_sleep_timeout > 0 else 0
        sleep_str = f"sleep: {sleep_min}m idle" if config.db_sleep_timeout > 0 else "sleep: off"
        print(f"║  DB: {db_label}  {sleep_str:<28}║")
        alert_label = f"ON (SoC>={config.alert_soc_threshold}%, ntfy/{config.ntfy_topic})" if config.ntfy_topic else "OFF (set NTFY_TOPIC in .env)"
        print(f"║  Alerts: {alert_label:<38}║")
        print("╠══════════════════════════════════════════════════════╣")
        print("║  [p]poll [i]interval [t]°F/°C [n]notify [d]docs [q]quit║")
        print("╚══════════════════════════════════════════════════════╝")
        print()

    # Initialize components
    display = ConsoleDisplay(tz_offset=config.tz_offset, temp_unit=config.temp_unit) if not headless else None
    storage = DataStorage(
        log_dir=config.log_dir,
        log_to_csv=config.log_to_csv,
        log_to_sqlite=config.log_to_sqlite,
        db_write_interval=config.db_write_interval,
        db_sleep_timeout=config.db_sleep_timeout,
    )
    notifier = Notifier(config)
    monitor = create_monitor(config)

    # Callback: handle new data from the monitor
    # Display updates on every event (real-time), but DB writes are
    # throttled to db_write_interval (default 5 min).
    async def on_data(data: F3800Data) -> None:
        if display:
            await display.update(data)
        await storage.write(data)
        await notifier.check_soc(data)
        await notifier.check_solar_done(data)

    monitor.add_callback(on_data)

    # Setup graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(sig, frame):
        logger.info("Received shutdown signal")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    if not headless:
        signal.signal(signal.SIGINT, signal_handler)

    monitor_task: asyncio.Task | None = None
    menu_task: asyncio.Task | None = None
    try:
        # Start storage and display
        await storage.start()
        if display:
            display.start()

        # Start monitor as a background task
        logger.info("Starting F3800 monitor...")
        monitor_task = asyncio.create_task(monitor.start())

        if not headless:
            # Start interactive menu as a background task
            menu_task = asyncio.create_task(
                _interactive_menu(monitor, display, config, notifier, shutdown_event)
            )

        # Wait for shutdown signal
        await shutdown_event.wait()

        # Signal the monitor to stop its internal loop
        monitor._running = False

    except ConnectionError as e:
        logger.error(f"Connection failed: {e}")
        if config.connection_mode == "modbus":
            print("\n💡 To use Modbus TCP, enable it in the Anker Solix app:")
            print("   1. Open the Anker Solix app on your iPhone")
            print("   2. Go to your F3800 device settings")
            print("   3. Look for 'Modbus TCP' and enable it")
            print("   4. Make sure the F3800 is on the same WiFi network")
    except Exception:
        logger.exception("Unexpected error")
    finally:
        # Cleanup
        logger.info("Shutting down...")
        monitor._running = False
        if display:
            display.stop()
        if not headless:
            if menu_task and not menu_task.done():
                menu_task.cancel()
        try:
            await monitor.stop()
        except Exception:
            pass
        # Cancel the monitor task if it's still running
        if monitor_task is not None and not monitor_task.done():
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        try:
            await storage.stop()
        except Exception:
            pass
        if not headless:
            print("\n👋 F3800 Monitor stopped.")


async def _interactive_menu(
    monitor: BaseMonitor,
    display: ConsoleDisplay,
    config: AppSettings,
    notifier: Notifier,
    shutdown_event: asyncio.Event,
) -> None:
    """Interactive menu loop — runs in the background, reads stdin.

    Commands:
        p — Force an immediate poll (send realtime trigger now)
        i — Change the polling interval
        t — Toggle temperature unit (°F / °C)
        n — Send a test push notification
        d — Open documentation viewer
        q — Quit the monitor
    """
    loop = asyncio.get_running_loop()

    while not shutdown_event.is_set():
        # Pause the live display so the input prompt is visible
        display.pause_live()

        # Read stdin without blocking the event loop
        try:
            cmd = await loop.run_in_executor(None, _read_input)
        except KeyboardInterrupt:
            # Ctrl+C was pressed inside input() — treat as quit
            display.resume_live()
            print()
            shutdown_event.set()
            break
        except (EOFError, asyncio.CancelledError):
            display.resume_live()
            break

        # Resume the live display so data updates render in-place
        display.resume_live()

        if cmd is None:
            # stdin closed (non-interactive mode) — exit menu loop
            # but keep the monitor running for data collection
            break

        cmd = cmd.strip().lower()

        if cmd == "p":
            # Force an immediate poll (also forces a DB write)
            if hasattr(monitor, "force_trigger"):
                monitor.force_trigger()
                # Force the next data callback to write to DB regardless of interval/sleep
                storage.reset_write_timer()
                if storage.sleeping:
                    storage.wake()  # Wake from sleep on explicit poll
                    print("  → Trigger sent, DB sleep mode overridden, waiting for data...")
                else:
                    print("  → Trigger sent, waiting for data (DB write forced)...")
            else:
                print("  → Force trigger not supported for this monitor type")

        elif cmd == "i":
            # Change polling interval
            try:
                display.pause_live()
                raw = await loop.run_in_executor(
                    None,
                    lambda: input("  New interval (minutes, e.g. 5 or 10): "),
                )
                display.resume_live()
                minutes = int(raw.strip())
                seconds = minutes * 60
                if hasattr(monitor, "set_poll_interval"):
                    monitor.set_poll_interval(seconds)
                    config.poll_interval = seconds
                    print(f"  → Poll interval set to {minutes}m ({seconds}s)")
                else:
                    print("  → Interval change not supported for this monitor type")
            except KeyboardInterrupt:
                print("  → Cancelled")
            except (ValueError, EOFError):
                print("  → Invalid input. Enter a number of minutes (e.g. 5, 10, 30).")

        elif cmd == "t":
            # Toggle temperature unit
            new_unit = "C" if config.temp_unit == "F" else "F"
            config.temp_unit = new_unit
            display.set_temp_unit(new_unit)
            print(f"  → Temperature unit: {new_unit}")

        elif cmd == "n":
            # Send test notification
            if notifier.enabled:
                print("  → Sending test notification...")
                success = await notifier.send_test()
                if success:
                    print("  → ✅ Test notification sent! Check your phone.")
                else:
                    print("  → ❌ Failed to send. Check NTFY_TOPIC and network.")
            else:
                print("  → Notifications not configured. Set NTFY_TOPIC in .env")

        elif cmd == "d":
            # Documentation viewer
            await _docs_menu(loop, display)

        elif cmd == "q":
            print("  → Shutting down...")
            shutdown_event.set()
            break

        elif cmd:
            print("  → Unknown command. Use: [p] poll  [i] interval  [t] °F/°C  [n] notify  [d] docs  [q] quit")


async def _docs_menu(loop: asyncio.AbstractEventLoop, display: ConsoleDisplay) -> None:
    """Interactive documentation viewer — show index, let user browse sections."""
    try:
        from src.docs import show_docs_index, show_section, SECTIONS
        from rich.console import Console
    except ImportError:
        print("  → Documentation requires the 'rich' library. Install with: pip install rich")
        return

    console = Console()
    show_docs_index(console)

    while True:
        try:
            display.pause_live()
            raw = await loop.run_in_executor(
                None,
                lambda: input("  Docs (1-6 or Enter to return): "),
            )
            display.resume_live()
        except (KeyboardInterrupt, EOFError):
            display.resume_live()
            break

        if not raw or not raw.strip():
            break  # Return to monitor

        key = raw.strip()
        if key in SECTIONS:
            show_section(key, console)
        else:
            print(f"  → Unknown section. Choose 1-{len(SECTIONS)} or press Enter to return.")


def _read_input() -> str | None:
    """Read a line from stdin (non-blocking compatible).

    Returns None on EOF (stdin closed) — the menu loop will
    detect this and exit gracefully so the monitor doesn't spin.
    """
    try:
        return input("> ")
    except EOFError:
        # Signal the menu to stop — stdin is closed (non-interactive mode)
        return None


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Anker Solix F3800 Monitor — Real-time telemetry from your F3800"
    )
    parser.add_argument(
        "--env",
        type=str,
        default=None,
        help="Path to .env configuration file",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["mqtt", "modbus"],
        default=None,
        help="Override connection mode from .env file",
    )
    parser.add_argument(
        "--device-sn",
        type=str,
        default=None,
        help="Override device serial number from .env file",
    )
    parser.add_argument(
        "--ip",
        type=str,
        default=None,
        help="Override F3800 IP address (for Modbus mode)",
    )
    parser.add_argument(
        "--no-csv",
        action="store_true",
        help="Disable CSV logging",
    )
    parser.add_argument(
        "--no-sqlite",
        action="store_true",
        help="Disable SQLite logging",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override poll interval in minutes (default: 10)",
    )
    parser.add_argument(
        "--db-interval",
        type=int,
        default=None,
        help="Override DB write interval in minutes (default: 5)",
    )
    parser.add_argument(
        "--sleep-timeout",
        type=int,
        default=None,
        help="Override DB sleep timeout in minutes (default: 30, 0=disabled)",
    )
    parser.add_argument(
        "--temp-unit",
        type=str,
        choices=["F", "C"],
        default=None,
        help="Override temperature unit: F (Fahrenheit) or C (Celsius)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without interactive menu or live display (for launchd/background use)",
    )

    args = parser.parse_args()

    # Load configuration
    config = AppSettings.from_env(args.env)

    # Apply CLI overrides
    if args.mode:
        config.connection_mode = args.mode
    if args.device_sn:
        config.device_sn = args.device_sn
    if args.ip:
        config.f3800_ip = args.ip
    if args.no_csv:
        config.log_to_csv = False
    if args.no_sqlite:
        config.log_to_sqlite = False
    if args.interval is not None:
        config.poll_interval = args.interval * 60  # Convert minutes to seconds
    if args.db_interval is not None:
        config.db_write_interval = args.db_interval * 60  # Convert minutes to seconds
    if args.sleep_timeout is not None:
        config.db_sleep_timeout = args.sleep_timeout * 60  # Convert minutes to seconds
    if args.temp_unit is not None:
        config.temp_unit = args.temp_unit.upper()

    # Run the async application
    asyncio.run(run_monitor(config, verbose=args.verbose, headless=args.headless))


if __name__ == "__main__":
    main()
