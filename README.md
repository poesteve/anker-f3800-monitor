# Anker Solix F3800 Monitor

Python tools for connecting to and monitoring the **Anker Solix F3800** portable power station on your local network.

## Features

- 🔋 Real-time battery SOC monitoring
- ⚡ Charging/discharging power tracking (AC input, DC/solar input, AC output)
- 📊 Per-port power readings (USB-C, USB-A, DC 12V)
- 🌡️ Device temperature monitoring
- 📝 Data logging to CSV and SQLite with throttled writes
- 😴 DB sleep mode — pauses logging when no solar/AC activity
- 🖥️ Rich console display with live updates (PV1 + PV2 breakdown)
- 📊 Daily summary script — solar energy, SoC range, max PV, charge/discharge totals
- 📈 Google Sheets export — daily summaries auto-pushed to a spreadsheet
- 🔌 Two connection modes: Cloud MQTT or Local Modbus TCP
- 🔔 Push notification alerts via ntfy.sh (SoC threshold + end-of-solar-day)

## Quick Start

### 1. Install dependencies

```bash
cd anker_f3800_monitor
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your Anker account credentials and device info
```

**Required for MQTT mode:**
- `ANKER_EMAIL` — Your Anker account email
- `ANKER_PASSWORD` — Your Anker account password
- `DEVICE_SN` — Your F3800's serial number (find in Anker app → Device Settings → About)

**Required for Modbus mode:**
- `F3800_IP` — Your F3800's IP address on your local network
- Enable Modbus TCP in the Anker Solix app (Device Settings)

### 3. Run

```bash
# Cloud MQTT mode (default)
python f3800_monitor.py

# Local Modbus TCP mode
python f3800_monitor.py --mode modbus

# With verbose logging
python f3800_monitor.py -v

# Disable logging
python f3800_monitor.py --no-csv --no-sqlite

# Custom DB write interval and sleep timeout
python f3800_monitor.py --db-interval 10 --sleep-timeout 60

# Disable DB sleep mode entirely
python f3800_monitor.py --sleep-timeout 0
```

## Connection Modes

| Mode | Protocol | Cloud Required? | Status |
|------|----------|-----------------|--------|
| **MQTT** | Cloud API + MQTT | Yes | ✅ Working (needs Anker account) |
| **Modbus** | Local Modbus TCP | No | ⚠️ Stub (needs Modbus enabled in Anker app + register map) |

### Cloud API + MQTT (Primary)

Uses the unofficial [`anker-solix-api`](https://github.com/thomluther/anker-solix-api) library to:
1. Authenticate with Anker's cloud API using your account credentials
2. Connect to Anker's AWS IoT MQTT broker with TLS client certificates
3. Subscribe to your F3800's data topics
4. Decode real-time hex messages into readable telemetry data

**Important limitations:**
- Only one active session per Anker account at a time
- If you open the Anker iOS app while the script is running, the script's session may be invalidated
- Device sharing is NOT available for the F3800 (no workaround using a second account)
- The API is unofficial and could break if Anker changes their cloud API

### Local Modbus TCP (Secondary)

The official Anker Home Assistant integration uses Modbus TCP for fully local communication. This client is a **stub** because:
- The F3800 does not expose Modbus TCP by default (port 502 is closed)
- No F3800 register map exists in the official HA integration yet
- You must enable Modbus TCP in the Anker app settings first

Once Modbus is enabled and the register map is determined, this client will work fully offline with no cloud dependency.

## Database Writing

By default, the database records a snapshot every **5 minutes** (12 data points/hour).
The real-time display updates on every MQTT event, but DB writes are throttled
to avoid excessive rows.

### DB Sleep Mode

If **PV1, PV2, and AC output** are all zero for longer than the sleep timeout
(default 30 min), DB writes pause automatically. This avoids logging noise
during nighttime when the battery is idle.

- Any PV/AC activity immediately resumes writes
- The display continues updating in real-time even while DB sleeps
- Press `p` (poll now) to force a DB write and wake from sleep
- Set `DB_SLEEP_TIMEOUT=0` to disable sleep mode

| Setting | .env Variable | Default | CLI Flag |
|---------|--------------|---------|----------|
| DB write interval | `DB_WRITE_INTERVAL` | 300s (5 min) | `--db-interval 5` (min) |
| DB sleep timeout | `DB_SLEEP_TIMEOUT` | 1800s (30 min) | `--sleep-timeout 30` (min) |

## Data Fields

The F3800 reports the following telemetry via MQTT:

| Field | Description | Unit |
|-------|-------------|------|
| `battery_soc` | Battery state of charge | % |
| `ac_input_power` | AC input (charging) power | W |
| `photovoltaic_power` | Total solar/PV input power | W |
| `pv_1_power` | PV1 (solar) input power | W |
| `pv_2_power` | PV2 (solar) input power | W |
| `output_power_total` | Total output power | W |
| `ac_output_power` | AC output power | W |
| `temperature` | Device temperature | °C |
| `charging_status` | Charging status code | — |
| `ac_output_power_switch` | AC output on/off | — |
| `usbc_1/2/3_power` | USB-C port power | W |
| `usba_1_power` | USB-A port power | W |
| `dc_12v_1_power` | DC 12V output power | W |

## Project Structure

```
anker_f3800_monitor/
├── .env.example          # Configuration template
├── .env                  # Your credentials (not tracked by git)
├── requirements.txt      # Python dependencies
├── README.md             # This file
├── f3800_monitor.py      # CLI entry point
├── daily_summary.py      # Daily metrics computation + Google Sheets export
├── check_db.py           # Quick SQLite data viewer (PV1/PV2 breakdown)
├── tests/                # Unit tests
│   ├── test_storage.py   # Storage throttling & sleep mode tests
│   └── test_daily_summary.py # Daily summary computation tests
├── data/                 # Logs directory (CSV, SQLite)
└── src/
    ├── __init__.py
    ├── config.py         # Configuration from .env
    ├── models.py         # F3800Data data model
    ├── storage.py        # CSV and SQLite logging (throttled + sleep mode)
    ├── display.py        # Console display (Rich or plain)
    ├── docs.py           # In-program documentation viewer
    └── clients/
        ├── __init__.py
        ├── base.py       # Abstract base monitor class
        ├── mqtt_cloud.py # Cloud API + MQTT client
        └── modbus_local.py # Local Modbus TCP client (stub)
```

## Daily Summary

The `daily_summary.py` script computes daily metrics from the SQLite database
and writes them to both a `daily_summary` table and optionally a Google Sheet.

```bash
python daily_summary.py                   # Summarize today
python daily_summary.py --yesterday       # Summarize yesterday
python daily_summary.py --date 2025-04-17 # Specific date
python daily_summary.py --dry-run         # Preview without writing
python daily_summary.py --no-sheets       # Skip Google Sheets upload
python daily_summary.py --backfill        # Populate sheet with all historical data from SQLite
```

### Metrics Computed

| Metric | Description |
|--------|-------------|
| Solar energy (Wh) | Total PV1+PV2 energy harvested (trapezoidal estimation) |
| PV1/PV2 energy (Wh) | Per-panel energy breakdown |
| Max PV1/PV2 (W) | Peak power captured on each panel |
| Peak solar time | Time of day when total PV was highest |
| Battery SoC | Start, end, min, max state of charge |
| Charge/discharge (Wh) | Total energy charged and discharged |
| AC input/output (Wh) | Total AC power in and out |
| Temperature | Min/max/avg in °C and °F |

### Google Sheets Setup

The script auto-detects the credential type from `credentials.json`:

**Service account (recommended for cron/automation):**
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable **Google Sheets API**
3. Go to **IAM & Admin → Service Accounts → Create Service Account**
4. Create a key (JSON) → Save as `credentials.json` in the project directory
5. **Share your Google Sheet** with the service account email (Editor access)
6. Copy the **spreadsheet ID** from the Sheet URL → Set `GOOGLE_SHEET_ID=<id>` in `.env`

**OAuth browser flow (for interactive/desktop use):**
1. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **Desktop app** → Download JSON → Save as `credentials.json`
3. Set `GOOGLE_SHEET_ID=<your-spreadsheet-id>` in `.env`
4. First run opens a browser for authorization; subsequent runs reuse `token.json`

> **Tip:** Service account auth is ideal for cron — no browser interaction needed.
> OAuth browser flow is fine for manual runs from your desktop.

### Auto-Run at 11:59PM (launchd)

A **launchd** plist is included for reliable daily scheduling on macOS.
It survives reboots and logs output to `data/logs/`.

```bash
# Install the launchd agent (one-time)
cp com.anker-f3800.daily-summary.plist ~/Library/LaunchAgents/
# Edit paths in the plist to match your setup before loading
nano ~/Library/LaunchAgents/com.anker-f3800.daily-summary.plist
launchctl load ~/Library/LaunchAgents/com.anker-f3800.daily-summary.plist

# Verify it's loaded
launchctl list | grep anker-f3800

# Uninstall if needed
launchctl unload ~/Library/LaunchAgents/com.anker-f3800.daily-summary.plist
rm ~/Library/LaunchAgents/com.anker-f3800.daily-summary.plist
```

The job runs at **11:59PM local system time** every day, capturing the
full day's data (solar + EV charging). Logs are written to:
- `data/logs/daily_summary.log` — standard output
- `data/logs/daily_summary.err` — errors

> **Note:** launchd uses the Mac's system timezone, not `TZ_OFFSET` from `.env`.
> If your Mac is set to PDT, 11:59PM = 11:59PM PDT.

**Alternative: cron** (if you prefer cron over launchd):
```bash
crontab -e
# Add:
59 23 * * * cd /path/to/anker_f3800_monitor && .venv/bin/python daily_summary.py >> data/logs/daily_summary.log 2>&1
```

### Missed Runs & Backfill

If your Mac is off or asleep at 11:59PM (e.g., faulty battery), the daily summary is missed.
You can catch up manually:

```bash
# Summarize a specific missed date
python daily_summary.py --date 2025-04-17

# Populate the Google Sheet with ALL historical summaries from SQLite
python daily_summary.py --backfill
```

The script uses **Append + UPSERT**: if a date already exists in the Google Sheet,
that row is updated in place; otherwise a new row is appended.
Existing rows for other dates are never touched or erased.

> **TODO:** Research migrating the daily summary to **Google Apps Script**
> so it runs server-side on a schedule (no Mac required). This would solve
> the missed-runs problem entirely and eliminate the need for a local launchd/cron job.

## Push Notification Alerts (ntfy.sh)

Get a push notification on your phone when battery SoC hits a threshold —
useful for knowing your battery will be full before the solar day is over.

### Setup

1. Install the **ntfy** app on your phone ([iOS](https://apps.apple.com/app/ntfy/id1625396365) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
2. Open the app → tap **+** → subscribe to a topic (e.g., `anker-f3800-alerts`)
   - Pick something unique/hard-to-guess — the topic acts as your channel
3. Add these to your `.env`:

```bash
NTFY_TOPIC=anker-f3800-alerts        # Your ntfy topic (must match phone subscription)
ALERT_SOC_THRESHOLD=90              # Alert when SoC >= this % (default: 90)
ALERT_COOLDOWN=3600                 # Min seconds between repeat alerts (default: 3600 = 1hr)
ALERT_SOLAR_END_TIME=19:30          # Solar-done alert after this local time (HH:MM)
```

4. Run the monitor — alerts will fire automatically when conditions are met
5. Press `n` in the monitor to send a **test notification** and verify it works

### Alert Types

**SoC Threshold Alert**
- Fires when battery SoC reaches `ALERT_SOC_THRESHOLD` (default 90%)
- **Cooldown**: Repeat alerts throttled by `ALERT_COOLDOWN` (default 1hr)
- **Auto-reset**: When SoC drops below the threshold, the alert resets

**Solar Done Alert**
- Fires once per evening when both PV1 and PV2 are 0W AND local time >= `ALERT_SOLAR_END_TIME`
- Includes current SoC, AC output, and temperature
- Lets you know solar day is over and where your battery stands for the evening
- One notification per day — won't repeat until the next evening

**Common**
- No signup needed: ntfy.sh is free, open-source, no account required (250 messages/day on public server)
- Notifications work even when the ntfy app isn't open (uses Apple/Google push notification infrastructure)

> **Note:** The solar-done alert uses `TZ_OFFSET` from `.env` to compute local time.
> When DST changes (PDT -7 ↔ PST -8), update `TZ_OFFSET` in `.env` or the
> 19:30 comparison will be off by an hour.

## Finding Your Device Serial Number

1. Open the **Anker Solix** app on your iPhone
2. Tap on your **F3800** device
3. Tap the **gear icon** (⚙️) for device settings
4. Look for **About** or **Device Info** — the serial number starts with `A1783` or `A1790`

## Troubleshooting

### "No devices found in Anker account"
- Verify your email and password in `.env`
- Make sure your F3800 is set up in the Anker app
- Check that you're using the correct country code

### "MQTT broker connection failed"
- Check your internet connection
- The Anker cloud may be temporarily unavailable
- Try closing the Anker iOS app (it may be using the same session)

### "Cannot connect to F3800 at 10.0.0.52:502"
- Modbus TCP is not enabled on the F3800
- Enable it in the Anker app → Device Settings
- Verify the F3800 is on the same WiFi network

## References

- [thomluther/anker-solix-api](https://github.com/thomluther/anker-solix-api) — Unofficial Python API library
- [anker-charging/ha-anker-solix-official](https://github.com/anker-charging/ha-anker-solix-official) — Official HA integration (Modbus)
- [Anker Solix F3800](https://www.anker.com/solix-f3800) — Product page
