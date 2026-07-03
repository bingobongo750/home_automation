"""SQLite layer: schema, and all read/write helpers.

Includes a small key/value `settings` table (JSON values) used for alert
thresholds; defaults live in DEFAULT_THRESHOLDS below.

Every helper opens a short-lived connection so the serial thread, the plug
poller thread, and Flask request handlers can all touch the DB without
sharing connections across threads. WAL mode keeps readers and the single
writer-at-a-time from blocking each other.

Tables
------
readings        sensor time series from the Arduino (metric = temp/hum/lux/co2/motion)
devices         generic device registry (wifi_plug and wled_zone rows so far)
power_readings  plug power/state time series, keyed to devices.id
"""

import json
import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,           -- unix epoch seconds
    metric TEXT NOT NULL,           -- temp | hum | lux | co2 | motion
    value  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_metric_ts ON readings (metric, ts);

CREATE TABLE IF NOT EXISTS devices (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    type   TEXT NOT NULL,           -- wifi_plug | wled_zone
    ip     TEXT,
    room   TEXT,
    mode   TEXT,                    -- wled_zone only: manual | auto
    locked INTEGER NOT NULL DEFAULT 0  -- wifi_plug only: 1 blocks power-off without confirmation
);

CREATE TABLE IF NOT EXISTS power_readings (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    watts     REAL,
    relay_on  INTEGER               -- 0/1, plug relay state at poll time
);
CREATE INDEX IF NOT EXISTS idx_power_device_ts ON power_readings (device_id, ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL               -- JSON blob
);
"""

# Alert thresholds: a reading outside [min, max] flags its widget on the
# dashboard. None disables that bound. "power" applies to every plug's draw.
DEFAULT_THRESHOLDS = {
    "temp": {"min": 17.0, "max": 26.0},    # °C — comfortable room band
    "hum": {"min": 30.0, "max": 60.0},     # %RH — below: dry air, above: mold risk
    "lux": {"min": None, "max": None},     # off by default; set per taste
    "co2": {"min": None, "max": 1000.0},   # ppm — >1000 means ventilate
    "power": {"min": None, "max": 1800.0}, # W — sustained near-limit socket load
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# Seeded so the API works before physical provisioning. More plugs: add a
# row here (and an IP in .env) — the poller and dashboard pick them up.
PLUG_SEEDS = [
    ("Plug 1", config.MYSTROM_PLUG_IP, "Living Room"),
    ("Plug 2", config.MYSTROM_PLUG2_IP, "Unassigned"),
]

# Same idea for WLED lighting zones. More zones: add a row here (and an IP
# in .env) — the auto-lighting job and dashboard pick them up.
WLED_SEEDS = [
    ("Cupboard", config.WLED_CUPBOARD_IP, "Kitchen"),
    ("Table", config.WLED_TABLE_IP, "Living Room"),
]


def init_db() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        # migrations: columns added after the first schema revision
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
        if "mode" not in cols:
            conn.execute("ALTER TABLE devices ADD COLUMN mode TEXT")
        if "locked" not in cols:
            conn.execute("ALTER TABLE devices ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        # legacy name from the first schema revision
        conn.execute("UPDATE devices SET name = 'Plug 1' WHERE name = 'myStrom Plug'")
        for name, ip, room in PLUG_SEEDS:
            exists = conn.execute(
                "SELECT 1 FROM devices WHERE name = ?", (name,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO devices (name, type, ip, room) VALUES (?, 'wifi_plug', ?, ?)",
                    (name, ip, room),
                )
        for name, ip, room in WLED_SEEDS:
            exists = conn.execute(
                "SELECT 1 FROM devices WHERE name = ?", (name,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    """INSERT INTO devices (name, type, ip, room, mode)
                       VALUES (?, 'wled_zone', ?, ?, 'manual')""",
                    (name, ip, room),
                )


# ----------------------------------------------------------------- settings

def get_thresholds() -> dict:
    """Saved thresholds merged over the defaults (so new keys get defaults)."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'thresholds'").fetchone()
    saved = json.loads(row["value"]) if row else {}
    out = {}
    for key, default in DEFAULT_THRESHOLDS.items():
        entry = saved.get(key, {})
        out[key] = {"min": entry.get("min", default["min"]),
                    "max": entry.get("max", default["max"])}
    return out


def set_thresholds(thresholds: dict) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES ('thresholds', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (json.dumps(thresholds),),
        )


# ------------------------------------------------------------- sensor writes

def insert_reading(metric: str, value: float, ts: float | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO readings (ts, metric, value) VALUES (?, ?, ?)",
            (ts or time.time(), metric, value),
        )


def insert_power_reading(device_id: int, watts: float | None, relay_on: bool) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO power_readings (ts, device_id, watts, relay_on) VALUES (?, ?, ?, ?)",
            (time.time(), device_id, watts, 1 if relay_on else 0),
        )


# -------------------------------------------------------------- sensor reads

def latest_readings() -> dict:
    """Most recent value + timestamp per metric."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT metric, value, ts FROM readings
               WHERE id IN (SELECT MAX(id) FROM readings GROUP BY metric)"""
        ).fetchall()
    return {r["metric"]: {"value": r["value"], "ts": r["ts"]} for r in rows}


def metric_history(metric: str, since: float, max_points: int = 300) -> list[dict]:
    """Time series for one metric since a unix timestamp, downsampled by
    time-bucket averaging to at most ~max_points rows so 24h ranges do not
    ship tens of thousands of raw rows to the browser."""
    span = max(time.time() - since, 1.0)
    bucket = max(span / max_points, 1.0)
    with connect() as conn:
        rows = conn.execute(
            """SELECT CAST(ts / ? AS INTEGER) * ? AS bucket_ts,
                      AVG(value) AS value, MAX(value) AS max_value
               FROM readings WHERE metric = ? AND ts >= ?
               GROUP BY bucket_ts ORDER BY bucket_ts""",
            (bucket, bucket, metric, since),
        ).fetchall()
    # For motion, average would blur 0/1 events away; report bucket max instead.
    key = "max_value" if metric == "motion" else "value"
    return [{"ts": r["bucket_ts"], "value": round(r[key], 2)} for r in rows]


def metric_stats(metric: str) -> dict:
    """Summary stats for a widget's expanded view: 24h min/max/avg + 7d avg."""
    now = time.time()
    with connect() as conn:
        day = conn.execute(
            """SELECT MIN(value) AS mn, MAX(value) AS mx, AVG(value) AS av
               FROM readings WHERE metric = ? AND ts >= ?""",
            (metric, now - 86400),
        ).fetchone()
        week = conn.execute(
            "SELECT AVG(value) AS av FROM readings WHERE metric = ? AND ts >= ?",
            (metric, now - 7 * 86400),
        ).fetchone()

    def rnd(v):
        return round(v, 1) if v is not None else None

    return {
        "min_24h": rnd(day["mn"]),
        "max_24h": rnd(day["mx"]),
        "avg_24h": rnd(day["av"]),
        "avg_7d": rnd(week["av"]),
    }


def power_stats(device_id: int) -> dict:
    """24h/7d average draw plus an estimated 24h energy figure (average watts
    integrated over the hours actually covered by samples)."""
    now = time.time()
    with connect() as conn:
        day = conn.execute(
            """SELECT AVG(watts) AS av, MAX(ts) - MIN(ts) AS span
               FROM power_readings
               WHERE device_id = ? AND ts >= ? AND watts IS NOT NULL""",
            (device_id, now - 86400),
        ).fetchone()
        week = conn.execute(
            """SELECT AVG(watts) AS av FROM power_readings
               WHERE device_id = ? AND ts >= ? AND watts IS NOT NULL""",
            (device_id, now - 7 * 86400),
        ).fetchone()
    kwh = None
    if day["av"] is not None:
        hours = min((day["span"] or 0) / 3600, 24)
        kwh = round(day["av"] * hours / 1000, 3)
    return {
        "avg_24h_w": round(day["av"], 1) if day["av"] is not None else None,
        "kwh_24h": kwh,
        "avg_7d_w": round(week["av"], 1) if week["av"] is not None else None,
    }


def metric_daily_profile(metric: str, days: int = 7, bucket_minutes: int = 30) -> list[dict]:
    """Average value per time-of-day bucket over the last N days — the
    'typical day' curve the dashboard overlays on the 24h chart. `tod` is
    seconds since local midnight at the bucket center."""
    since = time.time() - days * 86400
    with connect() as conn:
        rows = conn.execute(
            """SELECT CAST((strftime('%H', ts, 'unixepoch', 'localtime') * 60 +
                            strftime('%M', ts, 'unixepoch', 'localtime')) / ? AS INTEGER) AS bucket,
                      AVG(value) AS value
               FROM readings WHERE metric = ? AND ts >= ?
               GROUP BY bucket ORDER BY bucket""",
            (bucket_minutes, metric, since),
        ).fetchall()
    half = bucket_minutes * 30  # half a bucket, in seconds
    return [
        {"tod": r["bucket"] * bucket_minutes * 60 + half, "value": round(r["value"], 2)}
        for r in rows
    ]


def motion_count(since: float) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM readings WHERE metric = 'motion' AND value = 1 AND ts >= ?",
            (since,),
        ).fetchone()
    return row["n"]


def motion_events(since: float, limit: int = 50) -> list[dict]:
    """Recent motion-detected timestamps (value 1 rows), newest first,
    collapsed so a continuous HIGH period reports once per report tick."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT ts FROM readings
               WHERE metric = 'motion' AND value = 1 AND ts >= ?
               ORDER BY ts DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
    return [{"ts": r["ts"]} for r in rows]


# -------------------------------------------------------------- device reads

def list_devices() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_device(device_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


def set_device_mode(device_id: int, mode: str) -> None:
    """wled_zone only: 'manual' (dashboard controls it) or 'auto' (the
    lighting job drives brightness from lux)."""
    with connect() as conn:
        conn.execute("UPDATE devices SET mode = ? WHERE id = ?", (mode, device_id))


def set_device_locked(device_id: int, locked: bool) -> None:
    """wifi_plug only: when locked, /toggle refuses to power the plug off
    without an explicit confirmation (see api.device_toggle)."""
    with connect() as conn:
        conn.execute("UPDATE devices SET locked = ? WHERE id = ?", (1 if locked else 0, device_id))


def latest_power(device_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT ts, watts, relay_on FROM power_readings
               WHERE device_id = ? ORDER BY id DESC LIMIT 1""",
            (device_id,),
        ).fetchone()
    return dict(row) if row else None


def power_history(device_id: int, since: float, max_points: int = 300) -> list[dict]:
    span = max(time.time() - since, 1.0)
    bucket = max(span / max_points, 1.0)
    with connect() as conn:
        rows = conn.execute(
            """SELECT CAST(ts / ? AS INTEGER) * ? AS bucket_ts,
                      AVG(watts) AS watts
               FROM power_readings WHERE device_id = ? AND ts >= ?
               GROUP BY bucket_ts ORDER BY bucket_ts""",
            (bucket, bucket, device_id, since),
        ).fetchall()
    return [
        {"ts": r["bucket_ts"], "watts": round(r["watts"], 2) if r["watts"] is not None else None}
        for r in rows
    ]
