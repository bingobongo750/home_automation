"""SQLite layer: schema, and all read/write helpers.

Includes a small key/value `settings` table (JSON values) used for alert
thresholds, the active house-mode scene, and the last overnight summary;
threshold defaults live in DEFAULT_THRESHOLDS below.

Every helper opens a short-lived connection so the serial thread, the plug
poller thread, and Flask request handlers can all touch the DB without
sharing connections across threads. WAL mode keeps readers and the single
writer-at-a-time from blocking each other.

Tables
------
readings        sensor time series from the Arduino (metric = temp/hum/lux/co2/motion)
devices         generic device registry (wifi_plug and bulb_zone rows so far)
power_readings  plug power/state time series, keyed to devices.id
scenes          house modes (Sleeping/Home/Away): per-device target states as JSON

The planner's tables (events, tasks) are owned by app/planner.py — that
module carries its own DDL and init_db(), called alongside this one.
"""

import json
import sqlite3
import time
from datetime import datetime
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
    type   TEXT NOT NULL,           -- wifi_plug | bulb_zone
    ip     TEXT,
    room   TEXT,                    -- the hub covers a single room, so this is
                                    -- seeded empty; kept for a future multi-room hub
    mode   TEXT,                    -- bulb_zone only: manual | auto
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

CREATE TABLE IF NOT EXISTS scenes (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL UNIQUE,
    states TEXT NOT NULL              -- JSON: device name -> partial target state
);

-- One row per Sleeping window, written at the Sleeping -> Home transition.
-- settings.last_sleep_summary only ever holds the most recent night; this is
-- what makes a history reviewable. Keyed by the WAKE morning's local date, the
-- same anchor app/health.py uses for its nights, so the two line up.
CREATE TABLE IF NOT EXISTS night_summaries (
    night    TEXT PRIMARY KEY,        -- local "YYYY-MM-DD" of the morning
    from_ts  REAL NOT NULL,
    to_ts    REAL NOT NULL,
    summary  TEXT NOT NULL            -- the same JSON as last_sleep_summary
);
CREATE INDEX IF NOT EXISTS idx_night_from ON night_summaries (from_ts);
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
# Every device lives in the one room this hub covers, so `room` is seeded
# empty and the dashboard shows the device name alone.
PLUG_SEEDS = [
    ("Coffee machine", config.MYSTROM_PLUG_IP, ""),   # .51, physically installed
    ("Desk", config.MYSTROM_PLUG2_IP, ""),            # .52
]

# Same idea for Shelly bulb lighting zones. More zones: add a row here (and
# an IP in .env) — the auto-lighting job and dashboard pick them up.
BULB_SEEDS = [
    ("Cupboard", config.SHELLY_CUPBOARD_IP, ""),
    ("Room LED", config.SHELLY_ROOM_LED_IP, ""),
]

# Default house-mode scenes (see app/scenes.py). States are keyed either by
# a group key — "all_plugs" / "all_zones", applying to EVERY device of that
# type, present and future — or by a device *name* (the stable seed key
# above), which overrides the group's fields for that one device. Each value
# is a partial target; fields a scene doesn't mention are left alone. Rows
# are only inserted when missing, so edits to a scene's states in the DB
# survive restarts. Note "Home" deliberately has no zone targets: activating
# it lifts the auto-lighting suppression, so any zone whose mode is 'auto'
# resumes lux-driven brightness, and 'manual' zones stay as they are.
# Locked plugs are always skipped, whatever the scene says.
#
# "Home" was called "Day" until the presence module made the name wrong: it is
# the scene for "someone is in and awake", which is most of the evening too.
# Time of day is the *nightly schedule's* business, not this scene's.
SCENE_SEEDS = {
    "Sleeping": {
        "all_plugs": {"on": False},
        "all_zones": {"on": False},
    },
    "Home": {
        "all_plugs": {"on": True},
    },
    "Away": {
        "all_plugs": {"on": False},
        "all_zones": {"on": False},
    },
}

# Earlier seed revisions (night-light Sleeping, then per-name device lists).
# init_db swaps a stored row that still exactly matches any of these for the
# current SCENE_SEEDS entry — a hand-edited row is left alone. These keep the
# pre-rename "Table" and "Plug 1"/"Plug 2" names on purpose: they're matched
# against what an older DB actually stored, not against the current seeds.
# (Current seeds key off all_plugs/all_zones, so no live scene names a plug.)
_LEGACY_SCENE_STATES = {
    "Sleeping": [
        {"Plug 1": {"on": False},
         "Cupboard": {"on": True, "brightness": 40, "color": [255, 120, 40]},
         "Table": {"on": False}},
        {"Plug 1": {"on": False}, "Cupboard": {"on": False}, "Table": {"on": False}},
    ],
    # keyed by the CURRENT name — the Day -> Home rename below runs before this
    # matcher, so a legacy-stated row is already called "Home" by the time we
    # look for it
    "Home": [
        {"Plug 1": {"on": True}},
    ],
    "Away": [
        {"Plug 1": {"on": False}, "Cupboard": {"on": False}, "Table": {"on": False}},
    ],
}


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
        # legacy type from when the zones were WLED strips on ESP32s, before
        # the switch to self-contained Shelly bulbs (MANUAL.md 5.4)
        conn.execute("UPDATE devices SET type = 'bulb_zone' WHERE type = 'wled_zone'")
        # the second bulb zone is a room LED, not a table lamp; rename before
        # the seed loop below so it matches by name instead of inserting a twin
        conn.execute("UPDATE devices SET name = 'Room LED' WHERE name = 'Table'")
        # The plugs are named for what they run now that they're installed:
        # .51 is the coffee machine, .52 the desk. Same reason as the rename
        # above — do it before the seed loop, or the loop finds no "Coffee
        # machine" row and inserts a second plug on the same IP.
        conn.execute("UPDATE devices SET name = 'Coffee machine' WHERE name = 'Plug 1'")
        conn.execute("UPDATE devices SET name = 'Desk' WHERE name = 'Plug 2'")
        # single-room hub: the seeded room labels carried no information. Only
        # rows still on a seeded value are cleared, so a hand-set room survives.
        conn.execute(
            "UPDATE devices SET room = '' WHERE room IN ('Living Room', 'Kitchen', 'Unassigned')"
        )
        for name, ip, room in PLUG_SEEDS:
            exists = conn.execute(
                "SELECT 1 FROM devices WHERE name = ?", (name,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO devices (name, type, ip, room) VALUES (?, 'wifi_plug', ?, ?)",
                    (name, ip, room),
                )
        for name, ip, room in BULB_SEEDS:
            exists = conn.execute(
                "SELECT 1 FROM devices WHERE name = ?", (name,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    """INSERT INTO devices (name, type, ip, room, mode)
                       VALUES (?, 'bulb_zone', ?, ?, 'manual')""",
                    (name, ip, room),
                )
        # "Day" -> "Home". The scene means "someone is in and awake", which is
        # most of the evening too; time of day belongs to the nightly schedule.
        # Runs BEFORE the seed loop so an existing row is renamed rather than a
        # second one inserted alongside it, and before the legacy-revision
        # matcher below so that keys off the new name.
        conn.execute("UPDATE scenes SET name = 'Home' WHERE name = 'Day'")
        # ...and the persisted active scene, or the hub would come up pointing
        # at a scene that no longer exists and silently fall back to a default.
        active = conn.execute(
            "SELECT value FROM settings WHERE key = 'active_scene'").fetchone()
        if active:
            stored = json.loads(active["value"])
            if stored.get("name") == "Day":
                stored["name"] = "Home"
                conn.execute("UPDATE settings SET value = ? WHERE key = 'active_scene'",
                             (json.dumps(stored),))
        for name, states in SCENE_SEEDS.items():
            exists = conn.execute(
                "SELECT 1 FROM scenes WHERE name = ?", (name,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO scenes (name, states) VALUES (?, ?)",
                    (name, json.dumps(states)),
                )
        # rows still on an earlier seed revision -> current defaults
        for name, legacy_revisions in _LEGACY_SCENE_STATES.items():
            row = conn.execute(
                "SELECT states FROM scenes WHERE name = ?", (name,)
            ).fetchone()
            if row and json.loads(row["states"]) in legacy_revisions:
                conn.execute(
                    "UPDATE scenes SET states = ? WHERE name = ?",
                    (json.dumps(SCENE_SEEDS[name]), name),
                )
        # Scenes key device targets by name, so a hand-edited row naming the
        # old "Table" zone would silently stop matching after the rename above.
        # Runs last, so the legacy-revision match sees the pre-rename JSON.
        for row in conn.execute("SELECT id, states FROM scenes").fetchall():
            states = json.loads(row["states"])
            if "Table" in states:
                states["Room LED"] = states.pop("Table")
                conn.execute(
                    "UPDATE scenes SET states = ? WHERE id = ?",
                    (json.dumps(states), row["id"]),
                )


# ----------------------------------------------------------------- settings

def get_setting(key: str):
    """JSON value from the settings table, or None if the key isn't set."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def set_setting(key: str, value) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, json.dumps(value)),
        )


def get_thresholds() -> dict:
    """Saved thresholds merged over the defaults (so new keys get defaults)."""
    saved = get_setting("thresholds") or {}
    out = {}
    for key, default in DEFAULT_THRESHOLDS.items():
        entry = saved.get(key, {})
        out[key] = {"min": entry.get("min", default["min"]),
                    "max": entry.get("max", default["max"])}
    return out


def set_thresholds(thresholds: dict) -> None:
    set_setting("thresholds", thresholds)


def get_lighting() -> dict:
    """Auto-lighting settings, user-owned via the dashboard's settings dialog.

    `lux_off` is the ambient level at which a zone in 'auto' mode is fully
    off; app/lighting.py ramps brightness linearly from LIGHTING_AUTO_BRIGHTNESS
    at pitch dark down to nothing there. LIGHTING_LUX_THRESHOLD seeds it, so an
    untouched install behaves as the env var says."""
    saved = get_setting("lighting") or {}
    value = saved.get("lux_off")
    if value is None:
        value = config.LIGHTING_LUX_THRESHOLD
    return {"lux_off": float(value)}


def set_lighting(settings: dict) -> None:
    set_setting("lighting", settings)


# Nightly Sleeping window, applied by app/scenes.py. `sleep_time` activates
# Sleeping, `wake_time` hands back to Home (with the usual morning summary) —
# both plain local "HH:MM", both editable from the dashboard's settings dialog.
DEFAULT_SLEEP_SCHEDULE = {"enabled": True, "sleep_time": "00:00", "wake_time": "09:30"}


def get_sleep_schedule() -> dict:
    saved = get_setting("sleep_schedule") or {}
    return {key: saved.get(key, default)
            for key, default in DEFAULT_SLEEP_SCHEDULE.items()}


def set_sleep_schedule(schedule: dict) -> None:
    set_setting("sleep_schedule", schedule)


DEFAULT_PRESENCE = {"state": "home", "since": None,
                    "last_event": None, "last_event_at": None}


def get_presence() -> dict:
    """{"state": "home"|"away", "since", "last_event", "last_event_at"}.

    Persisted so presence survives a restart. A never-configured hub counts as
    "home" — the safe default, since the alternative is an empty-house scene on
    a box that has simply never been told anything (see app/presence.py)."""
    saved = get_setting("presence") or {}
    return {key: saved.get(key, default) for key, default in DEFAULT_PRESENCE.items()}


def set_presence(state: str, since: float | None,
                 last_event: str | None, last_event_at: float | None) -> None:
    set_setting("presence", {"state": state, "since": since,
                             "last_event": last_event,
                             "last_event_at": last_event_at})


def save_night_summary(from_ts: float, to_ts: float, summary: dict) -> None:
    """Record one night so it can be reviewed later. Keyed by the wake
    morning's local date; re-running a night replaces it rather than
    duplicating (a re-activated Sleeping keeps its original start)."""
    night = datetime.fromtimestamp(to_ts).strftime("%Y-%m-%d")
    with connect() as conn:
        conn.execute(
            """INSERT INTO night_summaries (night, from_ts, to_ts, summary)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(night) DO UPDATE SET
                   from_ts = excluded.from_ts,
                   to_ts   = excluded.to_ts,
                   summary = excluded.summary""",
            (night, from_ts, to_ts, json.dumps(summary)),
        )


def list_night_summaries(since: float | None = None) -> list[dict]:
    """Newest first. `summary` is the full stored payload per night."""
    sql = "SELECT night, from_ts, to_ts, summary FROM night_summaries"
    args: tuple = ()
    if since is not None:
        sql += " WHERE from_ts >= ?"
        args = (since,)
    sql += " ORDER BY from_ts DESC"
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{"night": r["night"], "from": r["from_ts"], "to": r["to_ts"],
             **json.loads(r["summary"])} for r in rows]


def get_night_summary(night: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT night, from_ts, to_ts, summary FROM night_summaries WHERE night = ?",
            (night,),
        ).fetchone()
    if row is None:
        return None
    return {"night": row["night"], "from": row["from_ts"], "to": row["to_ts"],
            **json.loads(row["summary"])}


def delete_night_summary(night: str) -> bool:
    """-> True if a row was removed. A stray Sleeping toggle records a junk
    few-minute 'night'; leaving it in skews the mean the anomaly flags are
    measured against, so it needs to be removable."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM night_summaries WHERE night = ?", (night,))
        return cur.rowcount > 0


def get_active_scene() -> dict | None:
    """{"name", "activated_at", "wake_time", "wake_at"} for the current house
    mode, or None if no scene has ever been activated (the backend treats
    that as "Home" — normal operation, auto lighting enabled)."""
    return get_setting("active_scene")


def set_active_scene(name: str, activated_at: float,
                     wake_time: str | None = None,
                     wake_at: float | None = None) -> None:
    """Persist the active scene (plus a pending Sleeping->Home wake, if any)
    so it survives backend restarts — app/scenes.py re-arms the wake timer
    from this record at startup."""
    set_setting("active_scene", {
        "name": name,
        "activated_at": activated_at,
        "wake_time": wake_time,   # "HH:MM" as entered, for display
        "wake_at": wake_at,       # resolved unix timestamp the timer fires at
    })


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


def motion_count(since: float, until: float | None = None) -> int:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM readings
               WHERE metric = 'motion' AND value = 1 AND ts >= ? AND ts <= ?""",
            (since, until if until is not None else time.time()),
        ).fetchone()
    return row["n"]


def motion_events(since: float, limit: int = 50, until: float | None = None) -> list[dict]:
    """Recent motion-detected timestamps (value 1 rows), newest first,
    collapsed so a continuous HIGH period reports once per report tick."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT ts FROM readings
               WHERE metric = 'motion' AND value = 1 AND ts >= ? AND ts <= ?
               ORDER BY ts DESC LIMIT ?""",
            (since, until if until is not None else time.time(), limit),
        ).fetchall()
    return [{"ts": r["ts"]} for r in rows]


def plug_window_summary(device_id: int, since: float, until: float) -> dict:
    """Peak draw plus the relay state at each end of a window, for the away
    summary — "did something switch on while I was out?". `changed` compares
    the first and last samples rather than counting transitions, because
    anything thermostatic (a fridge) cycles all day on its own."""
    with connect() as conn:
        agg = conn.execute(
            """SELECT MAX(watts) AS mx FROM power_readings
               WHERE device_id = ? AND ts >= ? AND ts <= ?""",
            (device_id, since, until),
        ).fetchone()
        first = conn.execute(
            """SELECT relay_on FROM power_readings
               WHERE device_id = ? AND ts >= ? AND ts <= ?
               ORDER BY ts ASC LIMIT 1""", (device_id, since, until)).fetchone()
        last = conn.execute(
            """SELECT relay_on FROM power_readings
               WHERE device_id = ? AND ts >= ? AND ts <= ?
               ORDER BY ts DESC LIMIT 1""", (device_id, since, until)).fetchone()
    start_on = bool(first["relay_on"]) if first else None
    end_on = bool(last["relay_on"]) if last else None
    return {
        "max_watts": round(agg["mx"], 1) if agg and agg["mx"] is not None else None,
        "start_on": start_on,
        "end_on": end_on,
        "changed": (start_on is not None and end_on is not None
                    and start_on != end_on),
    }


def metric_window_stats(metric: str, since: float, until: float) -> dict:
    """min/max/avg for one metric over an arbitrary window — used by the
    overnight (Sleeping->Home) summary in app/scenes.py."""
    with connect() as conn:
        row = conn.execute(
            """SELECT MIN(value) AS mn, MAX(value) AS mx, AVG(value) AS av
               FROM readings WHERE metric = ? AND ts >= ? AND ts <= ?""",
            (metric, since, until),
        ).fetchone()

    def rnd(v):
        return round(v, 1) if v is not None else None

    return {"min": rnd(row["mn"]), "max": rnd(row["mx"]), "avg": rnd(row["av"])}


def metric_window_endpoints(metric: str, since: float, until: float) -> tuple:
    """(first, last) reading values in a window, or (None, None) — used for
    the summary's CO2 start-vs-end trend."""
    with connect() as conn:
        first = conn.execute(
            """SELECT value FROM readings WHERE metric = ? AND ts >= ? AND ts <= ?
               ORDER BY ts ASC LIMIT 1""",
            (metric, since, until),
        ).fetchone()
        last = conn.execute(
            """SELECT value FROM readings WHERE metric = ? AND ts >= ? AND ts <= ?
               ORDER BY ts DESC LIMIT 1""",
            (metric, since, until),
        ).fetchone()
    return (first["value"] if first else None, last["value"] if last else None)


# --------------------------------------------------------------- scene reads

def list_scenes() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM scenes ORDER BY id").fetchall()
    return [{"id": r["id"], "name": r["name"], "states": json.loads(r["states"])}
            for r in rows]


def get_scene(name: str) -> dict | None:
    """Scene by name, case-insensitively ('sleeping' finds 'Sleeping')."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM scenes WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "states": json.loads(row["states"])}


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
    """bulb_zone only: 'manual' (dashboard controls it) or 'auto' (the
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
