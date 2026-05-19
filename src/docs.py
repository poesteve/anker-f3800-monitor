"""In-program documentation viewer for the F3800 Monitor."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table


# ── Documentation sections ──────────────────────────────────────────

_MENU_COMMANDS = """
## Menu Commands

| Key | Command | Description |
|-----|---------|-------------|
| `p` | **Poll now** | Force an immediate data poll from the F3800 |
| `i` | **Set interval** | Change the polling interval (in minutes) |
| `t` | **Toggle °F/°C** | Switch temperature display between Fahrenheit and Celsius |
| `d` | **Documentation** | Show this documentation viewer |
| `q` | **Quit** | Shut down the monitor |

Press a section number to jump to that topic, or `Enter` to return to the monitor.
"""

_SETTINGS = """
## Settings Reference

Settings can be configured in three ways (in order of priority):

1. **CLI flags** — override everything (e.g., `--interval 5`)
2. **`.env` file** — persistent defaults
3. **Built-in defaults** — used if nothing else is set

| Setting | .env Variable | Default | Description |
|---------|--------------|---------|-------------|
| Connection mode | `CONNECTION_MODE` | `mqtt` | `mqtt` (cloud) or `modbus` (local) |
| Anker email | `ANKER_EMAIL` | *(required)* | Your Anker account email |
| Anker password | `ANKER_PASSWORD` | *(required)* | Your Anker account password |
| Country | `ANKER_COUNTRY` | `US` | Country ID (US, DE, GB, etc.) |
| Device SN | `DEVICE_SN` | *(auto-detect)* | F3800 serial number |
| F3800 IP | `F3800_IP` | `10.0.0.52` | IP address (Modbus mode only) |
| Poll interval | `POLL_INTERVAL` | `600` | Seconds between polls (600 = 10 min) |
| DB write interval | `DB_WRITE_INTERVAL` | `300` | Seconds between DB writes (300 = 5 min, 12 pts/hr) |
| DB sleep timeout | `DB_SLEEP_TIMEOUT` | `1800` | Seconds idle before DB sleeps (1800 = 30 min) |
| Temperature unit | `TEMP_UNIT` | `F` | `F` (Fahrenheit) or `C` (Celsius) |
| Timezone offset | `TZ_OFFSET` | `-7` | Hours from UTC (PDT=-7, PST=-8) |
| Google Sheet ID | `GOOGLE_SHEET_ID` | *(empty)* | Spreadsheet ID for daily summary export |
| Log directory | `LOG_DIR` | `./data` | Where CSV/SQLite files are saved |
| CSV logging | `LOG_TO_CSV` | `true` | Enable/disable CSV logging |
| SQLite logging | `LOG_TO_SQLITE` | `true` | Enable/disable SQLite logging |

### CLI Flags

```
./run.sh                        # default 10-min polling, 5-min DB writes
./run.sh --interval 5           # poll every 5 minutes
./run.sh --db-interval 10       # write to DB every 10 minutes
./run.sh --sleep-timeout 60     # sleep after 60 min idle (0=disabled)
./run.sh --temp-unit C          # show °C instead of °F
./run.sh --mode modbus          # use local Modbus TCP
./run.sh --no-csv               # disable CSV logging
./run.sh --no-sqlite            # disable SQLite logging
./run.sh -v                     # verbose/debug logging
./run.sh --env /path/to/.env    # use a specific .env file
```
"""

_DATABASE = """
## Database Reference

### SQLite

- **File:** `data/f3800_log.db`
- **Table:** `f3800_telemetry`
- **Columns:** 56 (id + 55 data fields)
- **Write frequency:** Throttled to `DB_WRITE_INTERVAL` (default every 5 minutes = 12 pts/hr)
  - The display updates on every MQTT event (real-time), but DB writes are throttled
  - Use `p` (poll now) to force an immediate DB write (also wakes from sleep)
  - Set `DB_WRITE_INTERVAL` in `.env` or use `--db-interval` CLI flag to change
- **Sleep mode:** If PV1, PV2, and AC output are all zero for `DB_SLEEP_TIMEOUT` (default 30 min), DB writes pause
  - Any PV/AC activity immediately resumes writes
  - The display continues updating in real-time even while DB sleeps
  - Set `DB_SLEEP_TIMEOUT` in `.env` to change the timeout
- **Auto-migration:** Old schemas are automatically upgraded on startup

### Key Columns

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | TEXT | ISO 8601 UTC timestamp |
| `source` | TEXT | `mqtt` or `modbus` |
| `device_sn` | TEXT | F3800 serial number |
| `battery_soc` | INTEGER | Aggregate battery SoC (%) |
| `main_battery_soc` | INTEGER | Main unit battery SoC (%) |
| `charging_status` | INTEGER | Charging status code |
| `temperature` | REAL | Device temperature (°C) |
| `ac_input_power` | INTEGER | AC input power (W) |
| `photovoltaic_power` | INTEGER | Total solar/PV power (W) |
| `pv_1_power` | INTEGER | PV1 solar input (W) |
| `pv_2_power` | INTEGER | PV2 solar input (W) |
| `bat_charge_power` | INTEGER | Battery charging power (W) |
| `bat_discharge_power` | INTEGER | Battery discharging power (W) |
| `total_input_power` | INTEGER | Total input power (W) |
| `output_power_total` | INTEGER | Total output power (W) |
| `ac_output_power` | INTEGER | AC output power (W) |
| `remaining_time_hours` | REAL | Estimated time remaining (hrs) |
| `max_soc` | INTEGER | Max SoC limit setting (%) |
| `expansion_packs` | INTEGER | Number of expansion packs |
| `exp_N_sn` | TEXT | Expansion pack N serial number |
| `exp_N_soc` | INTEGER | Expansion pack N SoC (%) |
| `exp_N_soh` | INTEGER | Expansion pack N SoH (%) |
| `exp_N_temperature` | REAL | Expansion pack N temp (°C) |

### Daily Summary

- **Table:** `daily_summary` (in the same `f3800_log.db`)
- **Run:** `python daily_summary.py` (typically at 10PM via cron)
- **Metrics:** Solar energy (Wh), PV1/PV2 breakdown, max PV Watts, battery SoC range, charge/discharge energy, AC in/out energy, temperature range
- **Google Sheets:** If `GOOGLE_SHEET_ID` is set, summaries are also appended to a Google Sheet
  - Service account keys are auto-detected (no browser needed, ideal for cron)
  - OAuth browser flow is used as fallback for desktop credentials
  - Share the Google Sheet with the service account email (Editor access)
- **UPSERT:** Re-running for the same date replaces the existing summary row

### CSV

- **Files:** `data/f3800_log_YYYYMMDD.csv` (one per day)
- **Same columns** as SQLite, in the same order
- **Toggle:** `LOG_TO_CSV=true/false` in `.env`, or `--no-csv` CLI flag

### Querying SQLite

```bash
# Row count and date range
sqlite3 data/f3800_log.db \\
  "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM f3800_telemetry"

# Last 10 readings
sqlite3 data/f3800_log.db \\
  "SELECT timestamp, battery_soc, ac_output_power, photovoltaic_power
   FROM f3800_telemetry ORDER BY timestamp DESC LIMIT 10"

# Average SoC by hour
sqlite3 data/f3800_log.db \\
  "SELECT strftime('%H', timestamp) as hour,
          AVG(battery_soc) as avg_soc
   FROM f3800_telemetry GROUP BY hour ORDER BY hour"
```
"""

_MQTT_FIELDS = """
## MQTT Data Fields

The F3800 reports **72 raw MQTT keys** across two message types:
- `0405` — Core telemetry (battery, power, switches, USB, settings)
- `040a` — Expansion pack data (per-pack SoC, SoH, temp, SN)

### Battery / Status
| Field | Description | Unit |
|-------|-------------|------|
| `battery_soc` | Aggregate battery state of charge | % |
| `main_battery_soc` | Main unit battery SoC (excl. expansion) | % |
| `charging_status` | Charging status code | — |
| `temperature` | Device temperature | °C |
| `max_soc` | Maximum SoC limit setting | % |
| `remaining_time_hours` | Estimated time at current rate | hrs |

### Input Power
| Field | Description | Unit |
|-------|-------------|------|
| `ac_input_power` | AC input (charging) power | W |
| `photovoltaic_power` | Total solar/PV input power | W |
| `pv_1_power` | PV1 solar input power | W |
| `pv_2_power` | PV2 solar input power | W |
| `bat_charge_power` | Battery charging power | W |
| `bat_discharge_power` | Battery discharging power | W |

### Output Power
| Field | Description | Unit |
|-------|-------------|------|
| `ac_output_power` | AC output power | W |
| `output_power` | Total output power | W |
| `usbc_1/2/3_power` | USB-C port power | W |
| `usba_1/2_power` | USB-A port power | W |
| `dc_12v_1_power` | DC 12V output power | W |

### Switches / Controls
| Field | Description | Unit |
|-------|-------------|------|
| `ac_output_power_switch` | AC output on/off | — |
| `dc_output_power_switch` | DC output on/off | — |
| `ac_fast_charge_switch` | Fast charge on/off | — |
| `port_memory_switch` | Port memory on/off | — |

### Expansion Packs (per pack, 1–5)
| Field | Description | Unit |
|-------|-------------|------|
| `exp_N_sn` | Pack N serial number | — |
| `exp_N_soc` | Pack N state of charge | % |
| `exp_N_soh` | Pack N state of health | % |
| `exp_N_temperature` | Pack N temperature | °C |
| `exp_N_type` | Pack N model type | — |

### Hardware / Firmware
| Field | Description | Unit |
|-------|-------------|------|
| `device_sn` | Device serial number | — |
| `sw_version` | Firmware version | — |
| `region` | Device region | — |
"""

_CONNECTION_MODES = """
## Connection Modes

### Cloud MQTT (default)

Uses the unofficial `anker-solix-api` library to:
1. Authenticate with Anker's cloud API using your account credentials
2. Connect to Anker's AWS IoT MQTT broker with TLS client certificates
3. Subscribe to your F3800's data topics
4. Decode real-time hex messages into readable telemetry

**Limitations:**
- Requires internet and an Anker account
- Only one active session per account at a time
- If you open the Anker iOS app while the script runs, the session may be invalidated
- Device sharing is NOT available for the F3800
- Unofficial API — could break if Anker changes their cloud

### Local Modbus TCP (future)

The official Home Assistant integration uses Modbus TCP for fully local
communication. This client is a **stub** because:

- The F3800 does not expose Modbus TCP by default (port 502 is closed)
- You must enable Modbus TCP in the Anker app settings first
- No F3800 register map exists in the official HA integration yet

Once Modbus is enabled and the register map is determined, this client
will work fully offline with no cloud dependency.
"""

_TIPS = """
## Tips & Troubleshooting

### Running the Monitor
- Use `./run.sh` to start (automatically uses the correct Python venv)
- Run with `./run.sh --interval 1` for testing (1-minute polls)
- Use `./run.sh -v` for debug logging if something isn't working

### Session Conflicts
- If the Anker iOS app kicks the script off, just restart — it re-authenticates automatically
- Some reports indicate Anker has relaxed the single-session restriction

### Monitoring Schedule Suggestions
| Time (PDT) | Interval | Why |
|-------------|----------|-----|
| 6 AM – 8 PM | Every 5 min | Capture full solar curve + charging |
| 8 PM – 6 AM | Every 15 min | Track discharge / SoC decline |

### No Data Flowing?
1. Check your internet connection
2. Try closing the Anker iOS app (it may be using the same session)
3. Restart with `./run.sh -v` to see debug logs
4. The script auto-reconnects if the MQTT session drops

### Finding Your Device Serial Number
1. Open the **Anker Solix** app on your iPhone
2. Tap on your **F3800** device
3. Tap the **gear icon** (⚙️) for device settings
4. Look for **About** or **Device Info** — serial starts with `A1783` or `A1790`
"""

# Section registry
SECTIONS: dict[str, tuple[str, str]] = {
    "1": ("Menu Commands", _MENU_COMMANDS),
    "2": ("Settings Reference", _SETTINGS),
    "3": ("Database Reference", _DATABASE),
    "4": ("MQTT Data Fields", _MQTT_FIELDS),
    "5": ("Connection Modes", _CONNECTION_MODES),
    "6": ("Tips & Troubleshooting", _TIPS),
}


def show_docs_index(console: Console | None = None) -> None:
    """Show the documentation index with section choices."""
    if console is None:
        console = Console()

    table = Table(show_header=True, header_style="bold cyan", expand=True)
    table.add_column("#", style="bold", width=3)
    table.add_column("Section", style="bold")
    table.add_column("Description")

    descs = {
        "1": "Commands available at the > prompt",
        "2": "All configurable settings (.env, CLI, defaults)",
        "3": "SQLite & CSV file layout, columns, query examples",
        "4": "All 72 MQTT sensor fields the F3800 reports",
        "5": "Cloud MQTT vs Local Modbus TCP explained",
        "6": "Running, troubleshooting, monitoring tips",
    }
    for key, (title, _) in SECTIONS.items():
        table.add_row(key, title, descs.get(key, ""))

    console.print(Panel(
        table,
        title="📖  F3800 Monitor Documentation",
        border_style="blue",
        subtitle="Enter a section number, or press Enter to return",
    ))


def show_section(key: str, console: Console | None = None) -> bool:
    """Show a specific documentation section. Returns True if found."""
    if key not in SECTIONS:
        return False

    if console is None:
        console = Console()

    title, content = SECTIONS[key]
    console.print(Panel(
        Markdown(content),
        title=f"📖  {title}",
        border_style="cyan",
        subtitle="Enter another section #, or press Enter to return",
    ))
    return True
