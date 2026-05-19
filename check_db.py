#!/usr/bin/env python3
"""Quick check: show recent F3800 data from SQLite with PV1/PV2 breakdown."""

import sqlite3
from datetime import datetime, timezone, timedelta
from collections import Counter

PDT = timezone(timedelta(hours=-7))
db = sqlite3.connect("data/f3800_log.db")

# Total rows and range
c = db.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM f3800_telemetry")
count, mn, mx = c.fetchone()
dt_mn = datetime.fromisoformat(mn).astimezone(PDT)
dt_mx = datetime.fromisoformat(mx).astimezone(PDT)
print(f"Total rows: {count}")
print(f"Range: {dt_mn.strftime('%Y-%m-%d %H:%M:%S PDT')} to {dt_mx.strftime('%Y-%m-%d %H:%M:%S PDT')}")
print()

# Last 20 rows — now including PV1 and PV2
print("=== Last 20 rows ===")
c = db.execute("""
    SELECT timestamp, battery_soc, temperature, ac_input_power, ac_output_power,
           photovoltaic_power, pv_1_power, pv_2_power, bat_charge_power, bat_discharge_power
    FROM f3800_telemetry ORDER BY id DESC LIMIT 20
""")
rows = c.fetchall()
rows.reverse()
fmt = "{:<22s} {:>4s} {:>10s} {:>6s} {:>7s} {:>6s} {:>4s} {:>4s} {:>5s} {:>5s}"
print(fmt.format("PDT Time", "SoC", "Temp", "AC In", "AC Out", "Solar", "PV1", "PV2", "Chg", "Dis"))
print("-" * 88)
for r in rows:
    ts, soc, temp, ac_in, ac_out, pv, pv1, pv2, chg, dis = r
    dt = datetime.fromisoformat(ts).astimezone(PDT)
    t_str = dt.strftime("%H:%M:%S")
    t_disp = "{}C/{}F".format(temp, round(temp * 9 / 5 + 32)) if temp else "-"
    soc_s = str(soc) if soc is not None else "-"
    ac_in_s = str(ac_in) if ac_in is not None else "-"
    ac_out_s = str(ac_out) if ac_out is not None else "-"
    pv_s = str(pv) if pv is not None else "-"
    pv1_s = str(pv1) if pv1 is not None else "-"
    pv2_s = str(pv2) if pv2 is not None else "-"
    chg_s = str(chg) if chg is not None else "-"
    dis_s = str(dis) if dis is not None else "-"
    print(fmt.format(t_str, soc_s, t_disp, ac_in_s, ac_out_s, pv_s, pv1_s, pv2_s, chg_s, dis_s))

# Time gap stats
c = db.execute("SELECT timestamp FROM f3800_telemetry ORDER BY id")
all_ts = [r[0] for r in c.fetchall()]
gaps = []
for i in range(1, len(all_ts)):
    t1 = datetime.fromisoformat(all_ts[i - 1])
    t2 = datetime.fromisoformat(all_ts[i])
    gaps.append((t2 - t1).total_seconds())

print()
print("=== Time gap stats ({} intervals) ===".format(len(gaps)))
print("Min gap: {:.1f}s ({:.2f} min)".format(min(gaps), min(gaps) / 60))
print("Max gap: {:.1f}s ({:.2f} min)".format(max(gaps), max(gaps) / 60))
print("Avg gap: {:.1f}s ({:.2f} min)".format(sum(gaps) / len(gaps), sum(gaps) / len(gaps) / 60))

bucketed = Counter()
for g in gaps:
    if g < 10:
        bucketed["<10s"] += 1
    elif g < 30:
        bucketed["10-30s"] += 1
    elif g < 60:
        bucketed["30-60s"] += 1
    elif g < 300:
        bucketed["1-5min"] += 1
    elif g < 600:
        bucketed["5-10min"] += 1
    else:
        bucketed["10min+"] += 1
print("Distribution:")
for k in ["<10s", "10-30s", "30-60s", "1-5min", "5-10min", "10min+"]:
    v = bucketed.get(k, 0)
    pct = v / len(gaps) * 100
    print("  {:>10s}: {:5d} ({:.1f}%)".format(k, v, pct))

db.close()
