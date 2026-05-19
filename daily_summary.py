#!/usr/bin/env python3
"""Daily summary for Anker Solix F3800 telemetry.

Queries the SQLite database for a day's worth of 5-minute interval data,
computes summary metrics, and writes them to:
  1. A `daily_summary` table in the same SQLite database
  2. A Google Sheet (via service account or OAuth browser auth, if configured)

Designed to run automatically at 10PM via cron or launchd.

Usage:
    python daily_summary.py                   # Summarize today
    python daily_summary.py --date 2025-04-17 # Summarize a specific date
    python daily_summary.py --yesterday       # Summarize yesterday
    python daily_summary.py --dry-run         # Compute but don't write
    python daily_summary.py --no-sheets       # Skip Google Sheets upload
    python daily_summary.py -v                # Verbose (shows service account email)
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env from the project directory (works even when run via cron).

    Searches the script's parent directory and its parents, matching
    the logic used by the main monitor's AppSettings.from_env().
    Uses override=True so .env values take precedence over any
    system environment variables that might be set.
    """
    script_dir = Path(__file__).parent
    for candidate in (
        script_dir / ".env",               # anker_f3800_monitor/.env
        script_dir.parent / ".env",        # project root .env
        Path(".env"),                       # cwd .env (for manual runs)
    ):
        if candidate.exists():
            load_dotenv(candidate, override=True)
            return

    # Fallback: bare load_dotenv (finds .env in cwd)
    load_dotenv()


def _tz_from_env() -> timezone:
    """Build a timezone from TZ_OFFSET in .env (default PDT = -7)."""
    _load_env()
    offset_hours = int(os.getenv("TZ_OFFSET", "-7"))
    return timezone(timedelta(hours=offset_hours))


# ---------------------------------------------------------------------------
# Energy estimation (trapezoidal rule)
# ---------------------------------------------------------------------------

def _estimate_wh(rows: list[tuple], power_idx: int, ts_idx: int = 0) -> float:
    """Estimate energy in Watt-hours using the trapezoidal rule.

    Args:
        rows: Sorted list of (timestamp_str, ..., power_value, ...) tuples.
        power_idx: Index of the power column in each row.
        ts_idx: Index of the timestamp column (default 0).

    Returns:
        Estimated energy in Wh.
    """
    if len(rows) < 2:
        return 0.0

    total_wh = 0.0
    for i in range(1, len(rows)):
        t1_str = rows[i - 1][ts_idx]
        t2_str = rows[i][ts_idx]
        p1 = rows[i - 1][power_idx] or 0
        p2 = rows[i][power_idx] or 0

        t1 = datetime.fromisoformat(t1_str)
        t2 = datetime.fromisoformat(t2_str)
        dt_hours = (t2 - t1).total_seconds() / 3600.0

        # Skip gaps > 1 hour (likely sleep mode or downtime)
        if dt_hours > 1.0:
            continue

        # Trapezoidal: average power × time
        avg_power = (p1 + p2) / 2.0
        total_wh += avg_power * dt_hours

    return total_wh


# ---------------------------------------------------------------------------
# Compute daily summary
# ---------------------------------------------------------------------------

def compute_daily_summary(db_path: str, date: str, tz: timezone) -> dict[str, Any]:
    """Query SQLite and compute summary metrics for the given date.

    Args:
        db_path: Path to the f3800_log.db file.
        date: Date string in YYYY-MM-DD format (local tz).
        tz: Timezone for determining date boundaries.

    Returns:
        Dictionary of summary metrics, or empty dict if no data.
    """
    # Date boundaries in UTC for the SQL query
    local_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(timezone.utc)
    utc_end = local_end.astimezone(timezone.utc)

    db = sqlite3.connect(db_path)

    # Fetch all rows for the day, sorted by timestamp
    c = db.execute("""
        SELECT timestamp, main_battery_soc, temperature,
               ac_input_power, ac_output_power,
               photovoltaic_power, pv_1_power, pv_2_power,
               bat_charge_power, bat_discharge_power
        FROM f3800_telemetry
        WHERE timestamp >= ? AND timestamp < ?
        ORDER BY timestamp ASC
    """, (utc_start.isoformat(), utc_end.isoformat()))

    rows = c.fetchall()

    if not rows:
        db.close()
        return {}

    # Column indices
    i_ts, i_soc, i_temp, i_ac_in, i_ac_out, i_pv, i_pv1, i_pv2, i_chg, i_dis = range(10)

    # --- Battery SoC (main_battery_soc = matches F3800 physical display) ---
    socs = [r[i_soc] for r in rows if r[i_soc] is not None]
    soc_start = socs[0] if socs else None
    soc_end = socs[-1] if socs else None
    soc_min = min(socs) if socs else None
    soc_max = max(socs) if socs else None

    # --- Temperature ---
    temps = [r[i_temp] for r in rows if r[i_temp] is not None]
    temp_min = min(temps) if temps else None
    temp_max = max(temps) if temps else None
    temp_avg = round(sum(temps) / len(temps), 1) if temps else None

    # --- Solar energy (Wh) using trapezoidal rule ---
    solar_wh = round(_estimate_wh(rows, i_pv), 1)
    pv1_wh = round(_estimate_wh(rows, i_pv1), 1)
    pv2_wh = round(_estimate_wh(rows, i_pv2), 1)

    # --- Max PV power (W) ---
    pv1_vals = [r[i_pv1] for r in rows if r[i_pv1] is not None]
    pv2_vals = [r[i_pv2] for r in rows if r[i_pv2] is not None]
    max_pv1_w = max(pv1_vals) if pv1_vals else 0
    max_pv2_w = max(pv2_vals) if pv2_vals else 0
    max_pv_total_w = max(r[i_pv] for r in rows if r[i_pv] is not None) if any(r[i_pv] is not None for r in rows) else 0

    # --- Charge / discharge energy (Wh) ---
    charge_wh = round(_estimate_wh(rows, i_chg), 1)
    discharge_wh = round(_estimate_wh(rows, i_dis), 1)

    # --- AC input / output energy (Wh) ---
    ac_in_wh = round(_estimate_wh(rows, i_ac_in), 1)
    ac_out_wh = round(_estimate_wh(rows, i_ac_out), 1)

    # --- Data points and time range ---
    first_ts = rows[0][i_ts]
    last_ts = rows[-1][i_ts]
    data_points = len(rows)

    # --- Peak solar time (when total PV was highest) ---
    max_pv_row = max(rows, key=lambda r: r[i_pv] or 0)
    peak_solar_time = datetime.fromisoformat(max_pv_row[i_ts]).astimezone(tz).strftime("%H:%M")

    # --- Min solar time (first time both PV1 and PV2 are active together for over a minute) ---
    min_solar_time = "-"
    for j in range(1, len(rows)):
        prev = rows[j - 1]
        curr = rows[j]
        pv1_alive = (prev[i_pv1] or 0) > 1 and (curr[i_pv1] or 0) > 1
        pv2_alive = (prev[i_pv2] or 0) > 1 and (curr[i_pv2] or 0) > 1
        if pv1_alive and pv2_alive:
            min_solar_time = datetime.fromisoformat(prev[i_ts]).astimezone(tz).strftime("%H:%M")
            break

    # --- Post-solar SoC: SoC at the last PV-positive reading, confirmed by 60+ min idle ---
    soc_solar_end = None
    solar_end_time = "-"
    for j in range(len(rows) - 1, -1, -1):
        pv1 = rows[j][i_pv1] or 0
        pv2 = rows[j][i_pv2] or 0
        if pv1 > 0 or pv2 > 0:
            soc_solar_end = rows[j][i_soc]
            solar_end_time = datetime.fromisoformat(rows[j][i_ts]).astimezone(tz).strftime("%H:%M")
            break

    db.close()

    return {
        "date": date,
        "data_points": data_points,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "soc_start": soc_start,
        "soc_end": soc_end,
        "soc_min": soc_min,
        "soc_max": soc_max,
        "temp_min_c": temp_min,
        "temp_max_c": temp_max,
        "temp_avg_c": temp_avg,
        "solar_wh": solar_wh,
        "pv1_wh": pv1_wh,
        "pv2_wh": pv2_wh,
        "max_pv1_w": max_pv1_w,
        "max_pv2_w": max_pv2_w,
        "max_pv_total_w": max_pv_total_w,
        "peak_solar_time": peak_solar_time,
        "min_solar_time": min_solar_time,
        "solar_end_time": solar_end_time,
        "charge_wh": charge_wh,
"discharge_wh": discharge_wh,
        "ac_out_wh": ac_out_wh,
        "soc_solar_end": soc_solar_end,
    }


# ---------------------------------------------------------------------------
# SQLite summary table
# ---------------------------------------------------------------------------

SUMMARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    data_points INTEGER,
    first_ts TEXT,
    last_ts TEXT,
    soc_start INTEGER,
    soc_end INTEGER,
    soc_min INTEGER,
    soc_max INTEGER,
    temp_min_c REAL,
    temp_max_c REAL,
    temp_avg_c REAL,
    solar_wh REAL,
    pv1_wh REAL,
    pv2_wh REAL,
    max_pv1_w INTEGER,
    max_pv2_w INTEGER,
    max_pv_total_w INTEGER,
    peak_solar_time TEXT,
    min_solar_time TEXT,
    solar_end_time TEXT,
    charge_wh REAL,
discharge_wh REAL,
    ac_out_wh REAL,
    soc_solar_end INTEGER
);
"""


def write_summary_to_sqlite(db_path: str, summary: dict[str, Any]) -> None:
    """Write (or update) a daily summary row in SQLite."""
    db = sqlite3.connect(db_path)
    db.executescript(SUMMARY_SCHEMA)

    columns = [
        "date", "data_points", "first_ts", "last_ts",
        "soc_start", "soc_end", "soc_min", "soc_max",
        "temp_min_c", "temp_max_c", "temp_avg_c",
        "solar_wh", "pv1_wh", "pv2_wh",
        "max_pv1_w", "max_pv2_w", "max_pv_total_w",
        "peak_solar_time", "min_solar_time", "solar_end_time",
        "charge_wh", "discharge_wh",
        "ac_in_wh", "ac_out_wh",
        "soc_solar_end",
    ]

    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)

    # UPSERT: insert or replace on date conflict
    sql = f"INSERT OR REPLACE INTO daily_summary ({col_str}) VALUES ({placeholders})"
    values = tuple(summary.get(c) for c in columns)

    db.execute(sql, values)
    db.commit()
    db.close()
    logger.info("Wrote daily summary to SQLite for %s", summary["date"])


# ---------------------------------------------------------------------------
# Google Sheets export
# ---------------------------------------------------------------------------

# If modifying these scopes, delete the token.json file.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Column mapping for the Google Sheet: (internal_key, human_readable_header)
# Internal keys reference the summary dict; headers are written to row 1 of the sheet.
SHEET_COLUMNS: list[tuple[str, str]] = [
    # --- Primary columns (user-specified order) ---
    ("date",              "Date"),
    ("soc_start",         "SoC Start %"),
    ("soc_end",           "SoC End %"),
    ("soc_min",           "SoC Min %"),
    ("soc_max",           "SoC Max %"),
    ("solar_wh",          "Solar Energy Wh"),
    ("max_pv_total_w",    "Total PV Peak W"),
    ("peak_solar_time",   "Peak Solar Time"),
    ("min_solar_time",    "Min Solar Time"),
    ("solar_end_time",    "Solar End Time"),
    ("pv1_wh",            "PV1 (Patio) Energy Wh"),
    ("max_pv1_w",         "PV1 (Patio) Peak W"),
    ("pv2_wh",            "PV2 (Shed) Energy Wh"),
    ("max_pv2_w",         "PV2 (Shed) Peak W"),
    # --- Secondary columns (kept for review) ---
    ("temp_min_f",        "Temp Min °F"),
    ("temp_max_f",        "Temp Max °F"),
    ("temp_avg_f",        "Temp Avg °F"),
    ("discharge_wh",      "Discharged Wh"),
    ("ac_out_wh",         "AC Out Wh"),
    ("soc_solar_end",     "SoC Solar End %"),
]

# Convenience lists derived from SHEET_COLUMNS
SHEET_HEADERS = [header for _, header in SHEET_COLUMNS]
SHEET_KEYS = [key for key, _ in SHEET_COLUMNS]


def _detect_credential_type(credentials_path: str) -> str:
    """Detect whether a credentials JSON is a service account or OAuth client.

    Returns:
        'service_account' or 'oauth_client'
    """
    import json

    with open(credentials_path) as f:
        data = json.load(f)

    if data.get("type") == "service_account":
        return "service_account"
    return "oauth_client"


def _get_sheets_service_oauth(credentials_path: str, token_path: str):
    """Authenticate with Google Sheets API using OAuth browser flow.

    First run opens a browser for authorization. Subsequent runs use
    the saved token.json.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None

    # Load saved credentials if they exist
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # Refresh or create new credentials
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(credentials_path).exists():
                logger.error(
                    "Google credentials file not found: %s\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials",
                    credentials_path,
                )
                return None
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save credentials for next run
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("sheets", "v4", credentials=creds)


def _get_sheets_service_service_account(credentials_path: str):
    """Authenticate with Google Sheets API using a service account key.

    No browser interaction required — ideal for automated/cron usage.
    The service account email must be shared on the target Google Sheet.
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    if not Path(credentials_path).exists():
        logger.error(
            "Service account key not found: %s\n"
            "Download it from Google Cloud Console → IAM → Service Accounts → Keys",
            credentials_path,
        )
        return None

    creds = Credentials.from_service_account_file(
        credentials_path,
        scopes=SCOPES,
    )

    logger.info("Authenticated via service account: %s", creds.service_account_email)
    return build("sheets", "v4", credentials=creds)


def _get_sheets_service(credentials_path: str, token_path: str = ""):
    """Authenticate with Google Sheets API — auto-detects credential type.

    If credentials.json is a service account key (type=service_account),
    uses service account auth (no browser needed, ideal for cron).
    Otherwise, falls back to OAuth browser flow.
    """
    if not Path(credentials_path).exists():
        logger.error(
            "Google credentials file not found: %s\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials",
            credentials_path,
        )
        return None

    cred_type = _detect_credential_type(credentials_path)

    if cred_type == "service_account":
        logger.info("Detected service account credentials — using service account auth")
        return _get_sheets_service_service_account(credentials_path)
    else:
        logger.info("Detected OAuth client credentials — using browser auth flow")
        return _get_sheets_service_oauth(credentials_path, token_path)


def _build_data_row(summary: dict[str, Any]) -> list[str]:
    """Build a data row for the Google Sheet from a summary dict.

    Converts temperature from °C to °F for the sheet columns.
    """
    data_row = []
    for key in SHEET_KEYS:
        if key in ("temp_min_f", "temp_max_f", "temp_avg_f"):
            # Convert °C → °F: summary stores Celsius with _c suffix
            c_key = key.replace("_f", "_c")
            c_val = summary.get(c_key)
            if c_val is not None:
                data_row.append(str(round(c_val * 9 / 5 + 32, 1)))
            else:
                data_row.append("")
        else:
            val = summary.get(key)
            data_row.append(str(val if val is not None else ""))
    return data_row


def _find_date_row(sheet, spreadsheet_id: str, target_date: str) -> int | None:
    """Find the row number (1-based) containing target_date in column A.

    Returns None if the date is not found in the sheet.
    Row 1 is the header row; data rows start at row 2.
    """
    result = sheet.values().get(
        spreadsheetId=spreadsheet_id,
        range="A:A",
    ).execute()
    values = result.get("values", [])

    for i, row in enumerate(values):
        if row and row[0] == target_date:
            return i + 1  # Sheets API uses 1-based row numbers
    return None


def write_summary_to_sheets(
    summary: dict[str, Any],
    spreadsheet_id: str,
    credentials_path: str = "credentials.json",
    token_path: str = "token.json",
) -> bool:
    """Write a daily summary row to a Google Sheet (append or update-in-place).

    Uses Append + UPSERT strategy:
    - If the date already exists in the sheet, that row is updated in place.
    - If the date is new, a row is appended.
    - If the sheet is empty, headers are written first.
    - Existing rows for other dates are never touched or erased.

    Args:
        summary: Dictionary of summary metrics.
        spreadsheet_id: Google Sheets spreadsheet ID (from the URL).
        credentials_path: Path to Google credentials JSON (service account or OAuth client).
        token_path: Path to save/load OAuth token (only used for OAuth flow).

    Returns:
        True if successful, False otherwise.
    """
    try:
        service = _get_sheets_service(credentials_path, token_path)
        if service is None:
            return False

        sheet = service.spreadsheets()

        # Check if the sheet has headers already
        end_col = chr(ord("A") + len(SHEET_KEYS) - 1)
        result = sheet.values().get(
            spreadsheetId=spreadsheet_id,
            range=f"A1:{end_col}1",
        ).execute()

        values = result.get("values", [])

        if not values:
            # Sheet is empty — write human-readable headers first
            header_row = [SHEET_HEADERS]
            sheet.values().update(
                spreadsheetId=spreadsheet_id,
                range="A1",
                valueInputOption="RAW",
                body={"values": header_row},
            ).execute()
            logger.info("Wrote headers to Google Sheet")

        # Build the data row
        data_row = _build_data_row(summary)
        target_date = summary["date"]

        # Check if this date already exists in the sheet (UPSERT logic)
        existing_row = _find_date_row(sheet, spreadsheet_id, target_date)

        if existing_row is not None:
            # Update the existing row in place
            range_str = f"A{existing_row}:{end_col}{existing_row}"
            sheet.values().update(
                spreadsheetId=spreadsheet_id,
                range=range_str,
                valueInputOption="RAW",
                body={"values": [data_row]},
            ).execute()
            logger.info(
                "Updated existing row %d in Google Sheet for %s",
                existing_row, target_date,
            )
        else:
            # Append a new row
            sheet.values().append(
                spreadsheetId=spreadsheet_id,
                range="A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [data_row]},
            ).execute()
            logger.info("Appended new row to Google Sheet for %s", target_date)

        return True

    except Exception:
        logger.exception("Error writing to Google Sheets")
        return False


def backfill_sheets(
    db_path: str,
    spreadsheet_id: str,
    credentials_path: str = "credentials.json",
    token_path: str = "token.json",
) -> bool:
    """Populate the Google Sheet with all historical daily summaries from SQLite.

    Reads all rows from the daily_summary SQLite table and writes them
    to the Google Sheet. For dates already in the sheet, the row is
    updated; for new dates, a row is appended.

    Args:
        db_path: Path to SQLite database.
        spreadsheet_id: Google Sheets spreadsheet ID.
        credentials_path: Path to Google credentials JSON.
        token_path: Path to save/load OAuth token.

    Returns:
        True if successful, False otherwise.
    """
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SUMMARY_SCHEMA)
        cursor = db.execute(
            "SELECT * FROM daily_summary ORDER BY date ASC"
        )
        cols = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
    finally:
        db.close()

    if not rows:
        logger.warning("No daily_summary rows found in SQLite — nothing to backfill")
        return True

    logger.info("Backfilling %d days from SQLite to Google Sheet ...", len(rows))

    success = True
    for row in rows:
        summary = dict(zip(cols, row))
        result = write_summary_to_sheets(
            summary,
            spreadsheet_id=spreadsheet_id,
            credentials_path=credentials_path,
            token_path=token_path,
        )
        if not result:
            logger.error("Backfill failed for %s", summary.get("date", "?"))
            success = False

    if success:
        logger.info("Backfill complete — %d days written", len(rows))
    return success


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_summary(summary: dict[str, Any], tz: timezone) -> None:
    """Pretty-print the daily summary to the console."""
    date = summary["date"]
    soc_start = summary.get("soc_start", "-")
    soc_end = summary.get("soc_end", "-")
    soc_min = summary.get("soc_min", "-")
    soc_max = summary.get("soc_max", "-")
    soc_solar_end = summary.get("soc_solar_end", "-")

    temp_min_c = summary.get("temp_min_c")
    temp_max_c = summary.get("temp_max_c")
    temp_min_f = round(temp_min_c * 9 / 5 + 32) if temp_min_c is not None else "-"
    temp_max_f = round(temp_max_c * 9 / 5 + 32) if temp_max_c is not None else "-"

    solar_kwh = summary.get("solar_wh", 0) / 1000
    pv1_kwh = summary.get("pv1_wh", 0) / 1000
    pv2_kwh = summary.get("pv2_wh", 0) / 1000
    charge_kwh = summary.get("charge_wh", 0) / 1000
    discharge_kwh = summary.get("discharge_wh", 0) / 1000
    ac_in_kwh = summary.get("ac_in_wh", 0) / 1000
    ac_out_kwh = summary.get("ac_out_wh", 0) / 1000

    max_pv1 = summary.get("max_pv1_w", 0)
    max_pv2 = summary.get("max_pv2_w", 0)
    max_pv = summary.get("max_pv_total_w", 0)
    peak_time = summary.get("peak_solar_time", "-")
    min_time = summary.get("min_solar_time", "-")
    solar_end_time = summary.get("solar_end_time", "-")

    print()
    print(f"╔══════════════════════════════════════════════════════╗")
    print(f"║       F3800 Daily Summary — {date}              ║")
    print(f"╠══════════════════════════════════════════════════════╣")
    print(f"║  Data points: {summary.get('data_points', 0):<38}║")
    print(f"╠══════════════════════════════════════════════════════╣")
    print(f"║  [BAT] Battery SoC (main unit \u2014 matches F3800 display)  ║")
    print(f"║    Start: {str(soc_start):>3}%    End: {str(soc_end):>3}%                      ║")
    print(f"║    Min:   {str(soc_min):>3}%    Max: {str(soc_max):>3}%                      ║")
    print(f"║    Solar End: {str(soc_solar_end):>3}%                                      ║")
    print(f"╠══════════════════════════════════════════════════════╣")
    print(f"║  [SOL] Solar Energy                                  ║")
    print(f"║    Total: {solar_kwh:.2f} kWh  (Patio: {pv1_kwh:.2f}, Shed: {pv2_kwh:.2f})    ║")
    print(f"║    Peak: {max_pv}W at {peak_time}  (Patio: {max_pv1}W, Shed: {max_pv2}W)   ║")
    print(f"║    First Active: {min_time:<37}║")
    print(f"║    Last Active:  {solar_end_time:<37}║")
    print(f"╠══════════════════════════════════════════════════════╣")
    print(f"║  [CHG] Charge / Discharge                            ║")
    print(f"║    Charged:    {charge_kwh:.2f} kWh                            ║")
    print(f"║    Discharged: {discharge_kwh:.2f} kWh                            ║")
    print(f"╠══════════════════════════════════════════════════════╣")
    print(f"║  [AC]  AC Power                                      ║")
    print(f"║    AC In:  {ac_in_kwh:.2f} kWh                                ║")
    print(f"║    AC Out: {ac_out_kwh:.2f} kWh                                ║")
    print(f"╠══════════════════════════════════════════════════════╣")
    print(f"║  [TMP] Temperature                                   ║")
    print(f"║    {temp_min_f}F - {temp_max_f}F  ({temp_min_c}C - {temp_max_c}C)                ║")
    print(f"╚══════════════════════════════════════════════════════╝")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="F3800 Daily Summary — compute and export daily metrics",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date to summarize (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--yesterday",
        action="store_true",
        help="Summarize yesterday instead of today.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute summary but don't write to SQLite or Sheets.",
    )
    parser.add_argument(
        "--no-sheets",
        action="store_true",
        help="Skip Google Sheets upload.",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="data/f3800_log.db",
        help="Path to SQLite database (default: data/f3800_log.db).",
    )
    parser.add_argument(
        "--sheet-id",
        type=str,
        default=None,
        help="Google Sheets spreadsheet ID (overrides GOOGLE_SHEET_ID env var).",
    )
    parser.add_argument(
        "--credentials",
        type=str,
        default="credentials.json",
        help="Path to Google credentials JSON — service account or OAuth client (default: credentials.json).",
    )
    parser.add_argument(
        "--token",
        type=str,
        default="token.json",
        help="Path to save/load Google OAuth token (default: token.json).",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Backfill Google Sheet with all historical summaries from SQLite.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging.",
    )

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Timezone
    tz = _tz_from_env()

    # Determine date
    now_local = datetime.now(tz)
    if args.yesterday:
        date_str = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
    elif args.date:
        date_str = args.date
    else:
        date_str = now_local.strftime("%Y-%m-%d")

    # Compute summary
    logger.info("Computing daily summary for %s ...", date_str)
    summary = compute_daily_summary(args.db, date_str, tz)

    if not summary:
        print(f"❌ No data found for {date_str} in {args.db}")
        sys.exit(1)

    # Display
    print_summary(summary, tz)

    if args.dry_run:
        print("🏠 Dry run — not writing to SQLite or Sheets.")
        sys.exit(0)

    # Write to SQLite
    write_summary_to_sqlite(args.db, summary)
    print(f"✅ Summary written to SQLite (daily_summary table)")

    # Write to Google Sheets
    sheet_id = args.sheet_id or os.getenv("GOOGLE_SHEET_ID")
    if args.backfill and sheet_id and not args.no_sheets:
        success = backfill_sheets(
            args.db,
            spreadsheet_id=sheet_id,
            credentials_path=args.credentials,
            token_path=args.token,
        )
        if success:
            print(f"✅ Backfill complete — all historical summaries written to Google Sheet")
        else:
            print(f"⚠️  Backfill partially failed (check logs above)")
    elif sheet_id and not args.no_sheets:
        success = write_summary_to_sheets(
            summary,
            spreadsheet_id=sheet_id,
            credentials_path=args.credentials,
            token_path=args.token,
        )
        if success:
            print(f"✅ Summary written to Google Sheet")
        else:
            print(f"⚠️  Google Sheets upload failed (check logs above)")
    elif not sheet_id:
        print("ℹ️  GOOGLE_SHEET_ID not set — skipping Sheets upload")
    elif args.no_sheets:
        print("ℹ️  --no-sheets specified — skipping Sheets upload")


if __name__ == "__main__":
    main()
