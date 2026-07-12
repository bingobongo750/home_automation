#!/usr/bin/env python3
"""Seed the Health tab with realistic artificial nights so the dashboard can be
inspected before the real Apple Health feed (Tailscale + Health Auto Export) is
wired up. Temporary scaffolding — delete it once real data flows.

Fully reversible. Every night this creates is recorded in the `settings` table
under `health_seed_nights`; `--clear` deletes exactly those nights from every
health table and drops the marker. There's no real health data yet, so clearing
removes 100% of the artificial data and nothing else — and it stays safe after
the real feed connects, since it only ever touches the nights it created.

Data goes through the ordinary POST /api/health/ingest path (no Flask server or
hardware needed), so the full clean-RR -> metrics -> baselines -> scores pipeline
runs on it exactly as it will on real data.

Run from the server/ directory with the project venv:

    .venv/bin/python tools/seed_health.py             # ~30 nights (warms baselines)
    .venv/bin/python tools/seed_health.py --nights 7  # just a week (scores show provisional)
    .venv/bin/python tools/seed_health.py --clear     # remove every seeded night

Writes to config.DB_PATH — the same DB the server serves. If your server runs
with a non-default DB_PATH, set the same one here (env var) so they match.

Why ~30 nights by default: recovery/sleep baselines aren't trustworthy until
~14 nights of history (the scores render as "provisional" before then), so a
bare week would show a greyed-out, sparse dashboard. 30 nights warms the
baselines and fills the 30/60-day trend charts; the most recent 7 are your
realistic inspection week.
"""

import argparse
import math
import os
import random
import sys
from datetime import datetime, timedelta

# Import-time hardware clients stay mocked; this script only writes DB rows.
os.environ.setdefault("MOCK_HARDWARE", "1")
# Make `app` importable when run from server/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402

from app import config, db, health  # noqa: E402

SEED_MARKER = "health_seed_nights"
HEALTH_TABLES = (
    "health_rr", "health_sleep_stages", "health_sleep_hr", "health_night_samples",
    "health_night_metrics", "health_baselines", "health_scores",
    "health_sleep_scores", "health_subjective",
)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def hae(dt: datetime) -> str:
    """A local datetime as a Health Auto Export date string."""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


# --------------------------------------------------------- night generation

def _build_stages():
    """A believable hypnogram: 4-5 sleep cycles of Core/Deep/Core/REM with the
    occasional brief awakening — more deep early, more REM later. Returns
    (segments, total_minutes)."""
    segs, t = [], 0.0
    for cycle in range(random.choice([5, 5, 6, 6])):  # ~7-8.5h of sleep
        core1 = random.uniform(18, 28)
        segs.append(("Core", t, t + core1)); t += core1
        deep = random.uniform(22, 42) if cycle < 2 else random.uniform(8, 20)
        segs.append(("Deep", t, t + deep)); t += deep
        core2 = random.uniform(14, 24)
        segs.append(("Core", t, t + core2)); t += core2
        rem = random.uniform(10, 20) + 6 * cycle / 5
        segs.append(("REM", t, t + rem)); t += rem
        if random.random() < 0.55:
            awake = random.uniform(2, 9)
            segs.append(("Awake", t, t + awake)); t += awake
    return segs, t


def _rr_run(rr_mean, sigma, minutes):
    """A run of beat-to-beat RR intervals (ms) as a mean-reverting random walk,
    so successive-difference RMSSD lands near `sigma` — i.e. realistic HRV."""
    out, rr, t = [], rr_mean, 0.0
    while t < minutes * 60:
        rr += random.gauss(0, sigma) - 0.12 * (rr - rr_mean)
        rr = clamp(rr, 550, 1500)
        out.append(round(rr, 1))
        t += rr / 1000.0
    return out


def night_payload(bed: datetime) -> dict:
    """One night's Health-Auto-Export-shaped payload built around realistic
    per-night targets (HRV ~46 ms, resting HR ~53 bpm, resp ~14, etc.)."""
    segs, total = _build_stages()
    rmssd = clamp(random.gauss(46, 6), 28, 70)      # HRV target for the night
    hr_low = clamp(random.gauss(53, 2.5), 44, 64)   # nocturnal low
    rr_mean = 60000 / hr_low
    resp = round(clamp(random.gauss(14.2, 0.6), 11, 18), 1)
    spo2 = round(clamp(random.gauss(0.966, 0.008), 0.93, 0.99), 3)
    temp = round(clamp(random.gauss(34.8, 0.12), 34.2, 35.4), 2)

    stage_data = [{"startDate": hae(bed + timedelta(minutes=s)),
                   "endDate": hae(bed + timedelta(minutes=e)), "value": v}
                  for v, s, e in segs]
    rr_items = [{"date": hae(bed + timedelta(minutes=s)),
                 "intervals": _rr_run(rr_mean, rmssd, min(e - s, 4.0))}
                for v, s, e in segs if v == "Deep"]
    # sleep-HR curve dipping to the nightly low around mid-sleep
    hr = []
    for mm in range(5, int(total), 5):
        frac = mm / total
        val = hr_low + 6 * (1 - math.sin(math.pi * frac)) + random.gauss(0, 1.0)
        hr.append({"date": hae(bed + timedelta(minutes=mm)), "Avg": round(val, 1)})
    wake = bed + timedelta(minutes=total)
    return {"data": {"metrics": [
        {"name": "sleep_analysis", "data": stage_data},
        {"name": "rr_intervals", "data": rr_items},
        {"name": "heart_rate", "data": hr},
        {"name": "respiratory_rate", "data": [{"date": hae(wake), "qty": resp}]},
        {"name": "blood_oxygen_saturation", "data": [{"date": hae(wake), "qty": spo2}]},
        {"name": "apple_sleeping_wrist_temperature", "data": [{"date": hae(wake), "qty": temp}]},
    ]}}


# ----------------------------------------------------------- reversible marker

def _record_nights(nights):
    existing = set(db.get_setting(SEED_MARKER) or [])
    existing.update(nights)
    db.set_setting(SEED_MARKER, sorted(existing))


def clear():
    nights = db.get_setting(SEED_MARKER) or []
    if not nights:
        print("No seeded nights recorded — nothing to clear.")
        return
    placeholders = ",".join("?" * len(nights))
    with db.connect() as conn:
        for table in HEALTH_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE night IN ({placeholders})", nights)
        conn.execute("DELETE FROM settings WHERE key = ?", (SEED_MARKER,))
    health.recompute_all()  # rebuild baselines for any real nights left (none yet)
    print(f"Cleared {len(nights)} seeded night(s) from {config.DB_PATH}.")


# --------------------------------------------------------------------- seeding

def _seed_subjective(client):
    """A week of subjective 1-5 ratings that track the recovery score (with a
    little noise), so the Morning check-in correlation card demonstrates a
    believable positive relationship. Rank-mapped to spread across 1-5, since
    the recovery scores themselves cluster near the middle."""
    hist = client.get("/api/health/history?range=60d").get_json()["nights"]
    recent = [n for n in hist if n["recovery"] is not None][-7:]
    order = sorted(range(len(recent)), key=lambda i: recent[i]["recovery"])
    for rank, idx in enumerate(order):
        rating = clamp(round(1 + 4 * rank / max(len(recent) - 1, 1))
                       + random.choice([0, 0, 0, 1, -1]), 1, 5)
        client.post("/api/health/subjective",
                    json={"night": recent[idx]["night"], "rating": int(rating)})


def seed(nights: int):
    app = Flask(__name__)
    app.register_blueprint(health.bp)
    client = app.test_client()

    today = datetime.now().date()
    seeded = []
    for days_ago in range(nights - 1, -1, -1):   # oldest first (cheap baseline rebuilds)
        wake = today - timedelta(days=days_ago)
        bed = (datetime(wake.year, wake.month, wake.day, 23, 0) - timedelta(days=1)
               + timedelta(minutes=random.uniform(-40, 40)))
        resp = client.post("/api/health/ingest", json=night_payload(bed))
        if resp.status_code != 200:
            print("Ingest failed:", resp.get_json())
            sys.exit(1)
        seeded.append(health.night_of(bed.timestamp()))
    _seed_subjective(client)
    _record_nights(seeded)
    print(f"Inserted {len(seeded)} artificial night(s) into {config.DB_PATH}")
    print(f"Nights: {seeded[0]} … {seeded[-1]}")
    print("Start the server and open the Health tab (Board → Health).")
    print("Reverse any time with:  .venv/bin/python tools/seed_health.py --clear")


def main():
    parser = argparse.ArgumentParser(description="Seed/clear artificial Health data.")
    parser.add_argument("--nights", type=int, default=30,
                        help="how many nights to insert (default 30; <14 renders provisional)")
    parser.add_argument("--clear", action="store_true",
                        help="remove every night this script has seeded")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed for reproducible data")
    args = parser.parse_args()

    db.init_db()
    health.init_db()
    if args.clear:
        clear()
        return
    if args.seed is not None:
        random.seed(args.seed)
    seed(max(1, args.nights))


if __name__ == "__main__":
    main()
