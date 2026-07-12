"""Health: ingest + raw storage for Apple Health sleep/recovery data.

A self-contained module, like app/planner.py — its own tables (created by
init_db() here, not in db.SCHEMA), its own blueprint under /api/health.
Nothing in the device/scene lanes touches health data.

Raw ingest (pass 1) plus the derived pipeline (passes 2-4): clean RR ->
per-night metrics -> rolling baselines -> recovery score. Raw is kept forever
and is the source of truth; everything derived is recomputed from it (so a
weight change never needs a re-ingest). The math lives in health_compute.py
(pure, testable); this module owns ingest, persistence, and the endpoints.

The derived pipeline runs off ingest for the nights whose raw data just
changed, not a timer — data arrives by push, so this keeps the "Mac stays
thin" nightly-batch shape without a background thread. See recompute().

Ingest format — Health Auto Export ("JSON + CSV" iOS app) REST push:

    {"data": {"metrics": [{"name": ..., "units": ..., "data": [...]}, ...]}}

(a bare {"metrics": [...]} is accepted too). Recognized metric names:

- sleep_analysis        stage SEGMENTS: {"startDate", "endDate", "value":
                        "Awake"|"REM"|"Core"|"Deep"} — requires "aggregate
                        sleep data" OFF in the app; aggregate rows are
                        rejected with a pointed error. "In Bed"/"Asleep"
                        rows (stage-less sources) are skipped.
- heart_rate            {"date", "Avg"} or {"date", "qty"} → bpm samples
- respiratory_rate      {"date", "qty"} → nightly samples, metric resp_rate
- blood_oxygen_saturation                → nightly samples, metric spo2
- apple_sleeping_wrist_temperature       → nightly samples, metric wrist_temp
- rr_intervals / heartbeat_series        beat-to-beat RR intervals; either
                        {"date", "qty": <ms>} (one interval per item) or
                        {"date", "intervals": [<ms>, ...]} (a run starting
                        at date; each interval's ts advances by the previous
                        intervals). Not a stock Health Auto Export metric —
                        see docs/api.md for the expected shape.

Unknown metric names are skipped and reported back, never an error, so the
app can export more than we store. Malformed payloads (bad structure, bad
timestamps, non-numeric values in a recognized metric) are rejected with
400 and NOTHING from the payload is stored (single transaction).

Night convention: every row is keyed to a "night" — the LOCAL date of the
morning you woke up, assigned noon-to-noon (a sample between 12:00 on day D
and 12:00 on day D+1 belongs to night D+1). Same anchor the Sleep Regularity
Index uses later, so it never moves. Timestamps are stored raw alongside.

Re-ingesting an overlapping export is idempotent: exact duplicate rows are
dropped by UNIQUE indexes (INSERT OR IGNORE) and counted in the response.
"""

import json
import logging
import re
import time
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

from . import config, db
from . import health_compute as hc

log = logging.getLogger("health")

# Per-night derived metrics tracked with a rolling baseline. Recovery (pass 4)
# reads the first five; the sleep score (pass 5) compares tonight's REM/deep
# proportion and duration against the personal typical held in the last three.
BASELINE_METRICS = ("ln_rmssd", "rhr", "resp_rate", "wrist_temp", "spo2",
                    "tst_min", "rem_frac", "deep_frac")

# Sleep sub-score weights (sum 100 by default) — Duration dominant, stages
# modest per the measurement caveats (§2.2/§2.5). Kept in one dict so the UI
# breakdown and the scorer agree.
SLEEP_WEIGHTS = {
    "duration": config.HEALTH_SW_DURATION,
    "waso": config.HEALTH_SW_WASO,
    "consistency": config.HEALTH_SW_CONSISTENCY,
    "rem": config.HEALTH_SW_REM,
    "awakenings": config.HEALTH_SW_AWAKENINGS,
    "deep": config.HEALTH_SW_DEEP,
}

# Recovery-score config assembled from env (see config.HEALTH_*), passed into
# the pure model in health_compute so weights/penalties stay tunable.
SCORE_CFG = {
    "w_hrv": config.HEALTH_W_HRV,
    "w_rhr": config.HEALTH_W_RHR,
    "w_rr": config.HEALTH_W_RR,
    "temp_dev_c": config.HEALTH_TEMP_DEV_C,
    "spo2_dip_pct": config.HEALTH_SPO2_DIP_PCT,
    "rr_spike_br": config.HEALTH_RR_SPIKE_BR,
    "penalty_temp": config.HEALTH_PENALTY_TEMP,
    "penalty_spo2": config.HEALTH_PENALTY_SPO2,
    "penalty_rr": config.HEALTH_PENALTY_RR,
}

bp = Blueprint("health", __name__, url_prefix="/api/health")

SCHEMA = """
CREATE TABLE IF NOT EXISTS health_rr (
    id    INTEGER PRIMARY KEY,
    night TEXT NOT NULL,             -- "YYYY-MM-DD" local wake date (noon-to-noon)
    ts    REAL NOT NULL,             -- unix epoch seconds
    rr_ms REAL NOT NULL              -- one beat-to-beat interval, milliseconds
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_health_rr_dedupe ON health_rr (ts, rr_ms);
CREATE INDEX IF NOT EXISTS idx_health_rr_night ON health_rr (night);

CREATE TABLE IF NOT EXISTS health_sleep_stages (
    id       INTEGER PRIMARY KEY,
    night    TEXT NOT NULL,
    stage    TEXT NOT NULL,          -- awake | rem | core | deep
    start_ts REAL NOT NULL,
    end_ts   REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_health_stages_dedupe
    ON health_sleep_stages (start_ts, end_ts, stage);
CREATE INDEX IF NOT EXISTS idx_health_stages_night ON health_sleep_stages (night);

CREATE TABLE IF NOT EXISTS health_sleep_hr (
    id    INTEGER PRIMARY KEY,
    night TEXT NOT NULL,
    ts    REAL NOT NULL,
    bpm   REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_health_hr_dedupe ON health_sleep_hr (ts, bpm);
CREATE INDEX IF NOT EXISTS idx_health_hr_night ON health_sleep_hr (night);

CREATE TABLE IF NOT EXISTS health_night_samples (
    id     INTEGER PRIMARY KEY,
    night  TEXT NOT NULL,
    metric TEXT NOT NULL,            -- resp_rate | spo2 | wrist_temp
    ts     REAL NOT NULL,
    value  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_health_samples_dedupe
    ON health_night_samples (metric, ts, value);
CREATE INDEX IF NOT EXISTS idx_health_samples_night
    ON health_night_samples (night, metric);

-- Derived per-night metrics (pass 2): cleaned RMSSD/lnRMSSD, resting HR, and
-- the reduced nightly vitals. One row per night, recomputed from raw whenever
-- that night's raw data changes, so raw stays the source of truth.
CREATE TABLE IF NOT EXISTS health_night_metrics (
    night           TEXT PRIMARY KEY,
    rmssd           REAL,             -- ms, median across clean sleep windows
    ln_rmssd        REAL,             -- ln(rmssd) — baselines/z-scores use this
    rr_artifact_pct REAL,             -- % of beats corrected/dropped as artifact
    rr_windows      INTEGER,          -- clean windows that fed the night RMSSD
    rhr             REAL,             -- nocturnal resting HR (low percentile)
    resp_rate       REAL,             -- breaths/min, nightly median
    spo2            REAL,             -- %, nightly median (fractions normalised)
    wrist_temp      REAL,             -- degC, nightly median
    -- sleep-stage durations (pass 5), minutes unless noted
    tst_min         REAL,             -- total sleep time (rem+core+deep)
    tib_min         REAL,             -- time in bed (first segment .. last segment)
    waso_min        REAL,             -- wake after sleep onset (awake between onset/wake)
    awakenings      INTEGER,          -- awake segments between onset and final wake
    rem_min         REAL,
    deep_min        REAL,
    core_min        REAL,
    onset_ts        REAL,             -- first sleep-stage segment start
    wake_ts         REAL,             -- last sleep-stage segment end
    rem_frac        REAL,             -- rem_min / tst_min
    deep_frac       REAL,             -- deep_min / tst_min
    computed_at     REAL NOT NULL
);

-- Rolling baselines (pass 3): one snapshot per (night, metric) so a score can
-- be recomputed against the baseline as it stood on any night. Holds the
-- long-window anchor (mean/SD), the 7-day trend, the SWC band and CV.
CREATE TABLE IF NOT EXISTS health_baselines (
    night       TEXT NOT NULL,
    metric      TEXT NOT NULL,        -- ln_rmssd | rhr | resp_rate | wrist_temp | spo2
    mean        REAL,
    sd          REAL,
    trend_7     REAL,
    swc_low     REAL,
    swc_high    REAL,
    cv          REAL,
    n           INTEGER NOT NULL,     -- nights in the long window
    provisional INTEGER NOT NULL,     -- 1 until the warm-up count is reached
    PRIMARY KEY (night, metric)
);

-- Recovery scores (pass 4): the 0-100 score plus every intermediate (per-metric
-- z, weights, flags, penalty) so the UI can explain it and a weight change can
-- recompute it from stored metrics + baselines.
CREATE TABLE IF NOT EXISTS health_scores (
    night         TEXT PRIMARY KEY,
    recovery      REAL,               -- final 0-100 (base minus penalties)
    base_score    REAL,               -- before flag penalties
    z_total       REAL,
    contributions TEXT,               -- JSON: per-metric {z, weight, contribution}
    flags         TEXT,               -- JSON list: temp_deviation | spo2_dip | rr_spike
    penalty       REAL,
    provisional   INTEGER NOT NULL,
    computed_at   REAL NOT NULL
);

-- Subjective morning rating (pass 8): a 1-5 "how recovered do you feel" logged
-- each morning, correlated against the computed scores to guide weight tuning.
CREATE TABLE IF NOT EXISTS health_subjective (
    night      TEXT PRIMARY KEY,
    rating     INTEGER NOT NULL,     -- 1 (wrecked) .. 5 (great)
    note       TEXT,
    created_at REAL NOT NULL
);

-- Sleep score (pass 5) + deep-dive metrics (pass 6). subscores JSON holds each
-- component's {value, weight}; the deep-dive columns are display values judged
-- against personal baselines, not re-scored.
CREATE TABLE IF NOT EXISTS health_sleep_scores (
    night           TEXT PRIMARY KEY,
    sleep_score     REAL,             -- weighted 0-100
    subscores       TEXT,             -- JSON: duration/waso/consistency/rem/awakenings/deep
    recovery_index  TEXT,             -- JSON: sleep-HR-curve cross-signal, or null (§2.4)
    consistency_src TEXT,             -- 'sri' | 'sd_fallback' — what drove Consistency
    restorative_pct REAL,             -- (deep+rem)/tst, display only (pass 6)
    sleep_debt_min  REAL,             -- rolling need-minus-actual (pass 6)
    target_sleep_min REAL,            -- need + capped debt payback (pass 6)
    sri             REAL,             -- Sleep Regularity Index 0-100 (pass 6)
    provisional     INTEGER NOT NULL,
    computed_at     REAL NOT NULL
);
"""

# Sleep-stage columns added to health_night_metrics after the pass-2 schema —
# ALTERed in on an existing DB (mirrors the migration style in db.py/planner.py).
_SLEEP_METRIC_COLUMNS = [
    ("tst_min", "REAL"), ("tib_min", "REAL"), ("waso_min", "REAL"),
    ("awakenings", "INTEGER"), ("rem_min", "REAL"), ("deep_min", "REAL"),
    ("core_min", "REAL"), ("onset_ts", "REAL"), ("wake_ts", "REAL"),
    ("rem_frac", "REAL"), ("deep_frac", "REAL"),
]

# Health Auto Export metric name -> our health_night_samples.metric
SAMPLE_METRICS = {
    "respiratory_rate": "resp_rate",
    "blood_oxygen_saturation": "spo2",
    "apple_sleeping_wrist_temperature": "wrist_temp",
}

RR_METRIC_NAMES = ("rr_intervals", "heartbeat_series")

# sleep_analysis "value" -> stored stage; None = valid but not a stage
# segment we keep ("Asleep"/"In Bed" come from stage-less sources)
STAGE_VALUES = {
    "awake": "awake", "rem": "rem", "core": "core", "deep": "deep",
    "asleep": None, "inbed": None, "in bed": None,
}


def init_db() -> None:
    """Create the health tables. Called from create_app() right after
    db.init_db() (and from the test suite) — health owns its own DDL."""
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        # migration: sleep-stage columns added to health_night_metrics (pass 5)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(health_night_metrics)")}
        for col, decl in _SLEEP_METRIC_COLUMNS:
            if col not in cols:
                conn.execute(f"ALTER TABLE health_night_metrics ADD COLUMN {col} {decl}")


class BadPayload(ValueError):
    """Raised anywhere during ingest parsing — aborts the transaction and
    turns into a 400 with the message. Loud rejection over silent repair."""


# ------------------------------------------------------------------ helpers

def night_of(ts: float) -> str:
    """The night a timestamp belongs to: the LOCAL wake-morning date,
    noon-to-noon (12:00 day D .. 12:00 day D+1 -> night D+1)."""
    return (datetime.fromtimestamp(ts) + timedelta(hours=12)).date().isoformat()


def _parse_ts(value, field: str) -> float:
    """Epoch seconds from a number, a Health Auto Export date string
    ("2026-07-08 23:12:00 +0200"), or an ISO string (naive = local time,
    like everywhere else in the app)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M %z"):
            try:
                return datetime.strptime(s, fmt).timestamp()
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            pass
    raise BadPayload(f"{field}: unparseable timestamp {value!r}")


def _parse_num(value, field: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise BadPayload(f"{field}: expected a number, got {value!r}")


def _items(metric: dict) -> list:
    items = metric.get("data")
    if not isinstance(items, list):
        raise BadPayload(f"metric {metric.get('name')!r}: 'data' must be a list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise BadPayload(f"metric {metric.get('name')!r} item {i}: expected an object")
    return items


def _insert(conn, sql: str, params: tuple) -> int:
    """INSERT OR IGNORE, returning 1 if the row was new (0 = exact dup)."""
    return conn.execute(sql, params).rowcount


# ------------------------------------------------------------ metric parsers
# Each takes (conn, metric dict, affected set) and returns (stored,
# duplicates); a newly-inserted row's night is added to `affected` so ingest
# knows which nights' derived metrics to recompute afterwards.

def _ingest_sleep_stages(conn, metric: dict, affected: set) -> tuple[int, int]:
    stored = dups = 0
    for i, item in enumerate(_items(metric)):
        where = f"sleep_analysis item {i}"
        if "startDate" not in item or "endDate" not in item or "value" not in item:
            raise BadPayload(
                f"{where}: expected a stage segment with startDate/endDate/value — "
                "disable 'aggregate sleep data' in Health Auto Export")
        raw_stage = str(item["value"]).strip().lower()
        if raw_stage not in STAGE_VALUES:
            raise BadPayload(f"{where}: unknown sleep stage {item['value']!r}")
        stage = STAGE_VALUES[raw_stage]
        if stage is None:  # stage-less source rows — nothing to store
            continue
        start = _parse_ts(item["startDate"], f"{where} startDate")
        end = _parse_ts(item["endDate"], f"{where} endDate")
        if end <= start:
            raise BadPayload(f"{where}: endDate must be after startDate")
        night = night_of(start)
        n = _insert(conn,
                    """INSERT OR IGNORE INTO health_sleep_stages
                       (night, stage, start_ts, end_ts) VALUES (?, ?, ?, ?)""",
                    (night, stage, start, end))
        stored += n
        dups += 1 - n
        if n:
            affected.add(night)
    return stored, dups


def _ingest_heart_rate(conn, metric: dict, affected: set) -> tuple[int, int]:
    stored = dups = 0
    for i, item in enumerate(_items(metric)):
        where = f"heart_rate item {i}"
        ts = _parse_ts(item.get("date"), f"{where} date")
        bpm = _parse_num(item.get("Avg", item.get("qty")), f"{where} Avg/qty")
        night = night_of(ts)
        n = _insert(conn,
                    "INSERT OR IGNORE INTO health_sleep_hr (night, ts, bpm) VALUES (?, ?, ?)",
                    (night, ts, bpm))
        stored += n
        dups += 1 - n
        if n:
            affected.add(night)
    return stored, dups


def _ingest_night_samples(conn, metric: dict, our_name: str, affected: set) -> tuple[int, int]:
    stored = dups = 0
    for i, item in enumerate(_items(metric)):
        where = f"{metric.get('name')} item {i}"
        ts = _parse_ts(item.get("date"), f"{where} date")
        value = _parse_num(item.get("qty"), f"{where} qty")
        night = night_of(ts)
        n = _insert(conn,
                    """INSERT OR IGNORE INTO health_night_samples
                       (night, metric, ts, value) VALUES (?, ?, ?, ?)""",
                    (night, our_name, ts, value))
        stored += n
        dups += 1 - n
        if n:
            affected.add(night)
    return stored, dups


def _ingest_rr(conn, metric: dict, affected: set) -> tuple[int, int]:
    """Beat-to-beat RR intervals, milliseconds. Two item shapes: one
    interval per item ({"date", "qty"}), or a run ({"date", "intervals":
    [...]}) where each interval's timestamp advances by the ones before it."""
    stored = dups = 0
    for i, item in enumerate(_items(metric)):
        where = f"{metric.get('name')} item {i}"
        ts = _parse_ts(item.get("date"), f"{where} date")
        if "intervals" in item:
            intervals = item["intervals"]
            if not isinstance(intervals, list) or not intervals:
                raise BadPayload(f"{where}: 'intervals' must be a non-empty list")
            values = [_parse_num(v, f"{where} intervals[{j}]")
                      for j, v in enumerate(intervals)]
        else:
            values = [_parse_num(item.get("qty"), f"{where} qty")]
        for rr_ms in values:
            if rr_ms <= 0:
                raise BadPayload(f"{where}: RR interval must be positive, got {rr_ms}")
            night = night_of(ts)
            n = _insert(conn,
                        "INSERT OR IGNORE INTO health_rr (night, ts, rr_ms) VALUES (?, ?, ?)",
                        (night, ts, rr_ms))
            stored += n
            dups += 1 - n
            if n:
                affected.add(night)
            ts += rr_ms / 1000.0  # next beat lands after this interval
    return stored, dups


# ------------------------------------------------------- derived pipeline
# clean RR -> per-night metrics -> rolling baselines -> recovery score. Small,
# sequential, no real-time work (§3 nightly-batch model). Triggered off ingest
# for the nights whose raw data just changed rather than a timer, since data
# arrives by push — the same "Mac stays thin" property.

def _metrics_for_night(conn, night: str) -> dict:
    """Compute one night's derived metrics from its raw rows (pass 2)."""
    rr = conn.execute(
        "SELECT ts, rr_ms FROM health_rr WHERE night = ? ORDER BY ts", (night,)).fetchall()
    stages = conn.execute(
        "SELECT stage, start_ts, end_ts FROM health_sleep_stages WHERE night = ?",
        (night,)).fetchall()
    hr = conn.execute(
        "SELECT bpm FROM health_sleep_hr WHERE night = ?", (night,)).fetchall()
    samples = conn.execute(
        "SELECT metric, value FROM health_night_samples WHERE night = ?", (night,)).fetchall()

    hrv = hc.compute_hrv([(r["ts"], r["rr_ms"]) for r in rr],
                         [(s["stage"], s["start_ts"], s["end_ts"]) for s in stages])
    by_metric = {}
    for r in samples:
        by_metric.setdefault(r["metric"], []).append(r["value"])
    sleep = hc.sleep_stage_metrics(
        [(s["stage"], s["start_ts"], s["end_ts"]) for s in stages]) or {}
    return {
        "rmssd": hrv["rmssd"], "ln_rmssd": hrv["ln_rmssd"],
        "rr_artifact_pct": hrv["artifact_pct"], "rr_windows": hrv["windows"],
        "rhr": hc.resting_hr([r["bpm"] for r in hr]),
        "resp_rate": hc.median(by_metric.get("resp_rate", [])),
        "spo2": hc.normalize_spo2(hc.median(by_metric.get("spo2", []))),
        "wrist_temp": hc.median(by_metric.get("wrist_temp", [])),
        "tst_min": sleep.get("tst_min"), "tib_min": sleep.get("tib_min"),
        "waso_min": sleep.get("waso_min"), "awakenings": sleep.get("awakenings"),
        "rem_min": sleep.get("rem_min"), "deep_min": sleep.get("deep_min"),
        "core_min": sleep.get("core_min"), "onset_ts": sleep.get("onset_ts"),
        "wake_ts": sleep.get("wake_ts"), "rem_frac": sleep.get("rem_frac"),
        "deep_frac": sleep.get("deep_frac"),
    }


_METRIC_COLS = ("rmssd", "ln_rmssd", "rr_artifact_pct", "rr_windows", "rhr",
                "resp_rate", "spo2", "wrist_temp", "tst_min", "tib_min",
                "waso_min", "awakenings", "rem_min", "deep_min", "core_min",
                "onset_ts", "wake_ts", "rem_frac", "deep_frac")


def _upsert_night_metrics(conn, night: str) -> None:
    m = _metrics_for_night(conn, night)
    assignments = ", ".join(f"{c}=excluded.{c}" for c in _METRIC_COLS)
    placeholders = ", ".join("?" for _ in range(len(_METRIC_COLS) + 2))
    conn.execute(
        f"""INSERT INTO health_night_metrics
              (night, {', '.join(_METRIC_COLS)}, computed_at)
            VALUES ({placeholders})
            ON CONFLICT(night) DO UPDATE SET {assignments}, computed_at=excluded.computed_at""",
        (night, *(m[c] for c in _METRIC_COLS), time.time()))


def _baselines_for_night(history: list, night: str) -> dict:
    """Rolling baseline per tracked metric as of `night`: long-window anchor
    (30-60d mean/SD) + 7-day trend + SWC band. `history` is every
    health_night_metrics row; the window includes nights up to and including
    `night` (calendar-day distance, so gaps in the data don't distort it)."""
    nd = date.fromisoformat(night)
    long_days = config.HEALTH_BASELINE_LONG_DAYS
    short_days = config.HEALTH_BASELINE_SHORT_DAYS
    out = {}
    for metric in BASELINE_METRICS:
        long_vals, short_vals = [], []
        for row in history:
            delta = (nd - date.fromisoformat(row["night"])).days
            if delta < 0 or delta >= long_days:
                continue
            long_vals.append(row[metric])
            if delta < short_days:
                short_vals.append(row[metric])
        out[metric] = hc.baseline(long_vals, short_vals,
                                  config.HEALTH_BASELINE_WARMUP_NIGHTS)
    return out


def _sleep_need() -> float:
    """Personal sleep need in minutes — the user-set value if present, else the
    configured default (see PUT /api/health/settings)."""
    saved = db.get_setting("health_sleep_need_min")
    return float(saved) if saved else config.HEALTH_SLEEP_NEED_MIN


def _anchor_noon(night: str) -> float:
    """Local noon the day BEFORE the wake date — the noon-to-noon anchor a
    night's onset/wake times are measured from, so a bedtime near midnight
    doesn't wrap."""
    d = date.fromisoformat(night) - timedelta(days=1)
    return datetime(d.year, d.month, d.day, 12).timestamp()


def _consistency_sd_fallback(history: list, night: str):
    """SD-fallback Consistency sub-score from the trailing window's onset/wake
    times (pass 5; SRI supersedes it in pass 6)."""
    nd = date.fromisoformat(night)
    onset_offs, wake_offs = [], []
    for r in history:
        delta = (nd - date.fromisoformat(r["night"])).days
        if delta < 0 or delta >= config.HEALTH_SRI_WINDOW_DAYS:
            continue
        if r["onset_ts"] is None or r["wake_ts"] is None:
            continue
        anchor = _anchor_noon(r["night"])
        onset_offs.append((r["onset_ts"] - anchor) / 60.0)
        wake_offs.append((r["wake_ts"] - anchor) / 60.0)
    return hc.timing_consistency_subscore(onset_offs, wake_offs,
                                          config.HEALTH_CONS_SD_BAD_MIN)


def _sleep_subscores(row, baselines, need) -> dict:
    rem_typ = (baselines["rem_frac"]["mean"] if baselines.get("rem_frac")
               else config.HEALTH_REM_TYPICAL)
    deep_typ = (baselines["deep_frac"]["mean"] if baselines.get("deep_frac")
                else config.HEALTH_DEEP_TYPICAL)
    return {
        "duration": hc.duration_subscore(row["tst_min"], need,
                                         config.HEALTH_OVERSLEEP_TOL_MIN,
                                         config.HEALTH_OVERSLEEP_ZERO_MIN,
                                         config.HEALTH_DUR_SHORT_PENALTY_PER_H),
        "waso": hc.waso_subscore(row["waso_min"], config.HEALTH_WASO_GOOD_MIN,
                                 config.HEALTH_WASO_BAD_MIN),
        "rem": hc.stage_deficit_subscore(row["rem_frac"], rem_typ),
        "deep": hc.stage_deficit_subscore(row["deep_frac"], deep_typ),
        "awakenings": hc.awakenings_subscore(row["awakenings"],
                                             config.HEALTH_AWK_GOOD, config.HEALTH_AWK_BAD),
    }


def _trailing_tst(history: list, night: str, days: int) -> list:
    """TST (minutes) for the trailing `days` nights including `night`."""
    nd = date.fromisoformat(night)
    out = []
    for r in history:
        delta = (nd - date.fromisoformat(r["night"])).days
        if 0 <= delta < days and r["tst_min"] is not None:
            out.append(r["tst_min"])
    return out


def _compute_sri(conn, history: list, night: str):
    """Sleep Regularity Index over the 7-day noon-to-noon window ending at the
    wake-date noon. Returns None unless the window holds >= 2 nights of sleep
    (SRI off one night is meaningless — mostly awake-vs-awake concordance)."""
    nd = date.fromisoformat(night)
    if len([r for r in _trailing_tst(history, night, config.HEALTH_SRI_WINDOW_DAYS)]) < 2:
        return None
    window_end = datetime(nd.year, nd.month, nd.day, 12).timestamp()
    window_start = window_end - config.HEALTH_SRI_WINDOW_DAYS * 86400
    rows = conn.execute(
        """SELECT start_ts, end_ts FROM health_sleep_stages
           WHERE stage IN ('rem', 'core', 'deep') AND end_ts >= ? AND start_ts <= ?""",
        (window_start, window_end)).fetchall()
    spans = [(max(r["start_ts"], window_start), min(r["end_ts"], window_end)) for r in rows]
    spans = [s for s in spans if s[1] > s[0]]
    return hc.sleep_regularity_index(spans, window_start, window_end,
                                     config.HEALTH_SRI_EPOCH_SEC)


def _upsert_sleep_score(conn, history: list, row, baselines: dict) -> None:
    """Sleep score (pass 5) + deep-dive metrics (pass 6). Six weighted
    sub-scores -> 0-100, with Consistency driven by the SRI when the window has
    enough data (SD-of-timings fallback otherwise). Also stores the display-only
    deep-dive values: restorative %, rolling sleep debt, next-night target, SRI,
    and the sleep-HR-curve recovery index."""
    night = row["night"]
    if row["tst_min"] is None:  # no scored sleep this night
        conn.execute("DELETE FROM health_sleep_scores WHERE night = ?", (night,))
        return
    need = _sleep_need()

    # Consistency component: SRI when the window supports it, else SD fallback.
    sri = _compute_sri(conn, history, night)
    if sri is not None:
        consistency, consistency_src = sri, "sri"
    else:
        consistency, consistency_src = _consistency_sd_fallback(history, night), "sd_fallback"

    subs = _sleep_subscores(row, baselines, need)
    subs["consistency"] = consistency
    result = hc.sleep_score(subs, SLEEP_WEIGHTS)

    hr = conn.execute(
        "SELECT ts, bpm FROM health_sleep_hr WHERE night = ? ORDER BY ts", (night,)).fetchall()
    ri = hc.recovery_index([(r["ts"], r["bpm"]) for r in hr],
                           row["onset_ts"], row["wake_ts"])

    # deep-dive (pass 6) — display only, judged against personal baselines
    restorative = hc.restorative_pct(row["deep_min"], row["rem_min"], row["tst_min"])
    debt = hc.sleep_debt(_trailing_tst(history, night, config.HEALTH_SLEEP_DEBT_DAYS),
                         need, config.HEALTH_SLEEP_SURPLUS_DISCOUNT)
    target = hc.target_sleep(need, debt, config.HEALTH_SLEEP_PAYBACK_ALPHA,
                             config.HEALTH_SLEEP_PAYBACK_CAP_MIN)

    tst_base = baselines.get("tst_min")
    provisional = tst_base is None or tst_base["provisional"]
    conn.execute(
        """INSERT INTO health_sleep_scores
             (night, sleep_score, subscores, recovery_index, consistency_src,
              restorative_pct, sleep_debt_min, target_sleep_min, sri,
              provisional, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(night) DO UPDATE SET
             sleep_score=excluded.sleep_score, subscores=excluded.subscores,
             recovery_index=excluded.recovery_index, consistency_src=excluded.consistency_src,
             restorative_pct=excluded.restorative_pct, sleep_debt_min=excluded.sleep_debt_min,
             target_sleep_min=excluded.target_sleep_min, sri=excluded.sri,
             provisional=excluded.provisional, computed_at=excluded.computed_at""",
        (night, result["score"], json.dumps(result["subscores"]),
         json.dumps(ri), consistency_src, restorative, debt, target, sri,
         1 if provisional else 0, time.time()))


def _rebuild_baselines_and_scores(conn, since_night: str) -> None:
    """Recompute baselines + recovery score for every night on/after
    `since_night` (a changed metric there shifts every later night's rolling
    window; earlier nights are untouched — the window never looks forward)."""
    history = conn.execute(
        "SELECT * FROM health_night_metrics ORDER BY night").fetchall()
    for row in history:
        night = row["night"]
        if night < since_night:
            continue
        baselines = _baselines_for_night(history, night)
        for metric, base in baselines.items():
            if base is None:
                conn.execute("DELETE FROM health_baselines WHERE night = ? AND metric = ?",
                             (night, metric))
                continue
            conn.execute(
                """INSERT INTO health_baselines
                     (night, metric, mean, sd, trend_7, swc_low, swc_high, cv, n, provisional)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(night, metric) DO UPDATE SET
                     mean=excluded.mean, sd=excluded.sd, trend_7=excluded.trend_7,
                     swc_low=excluded.swc_low, swc_high=excluded.swc_high,
                     cv=excluded.cv, n=excluded.n, provisional=excluded.provisional""",
                (night, metric, base["mean"], base["sd"], base["trend_7"],
                 base["swc_low"], base["swc_high"], base["cv"], base["n"],
                 1 if base["provisional"] else 0))
        metrics = {k: row[k] for k in ("ln_rmssd", "rhr", "resp_rate", "wrist_temp", "spo2")}
        score = hc.recovery_score(metrics, baselines, SCORE_CFG)
        conn.execute(
            """INSERT INTO health_scores
                 (night, recovery, base_score, z_total, contributions, flags,
                  penalty, provisional, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(night) DO UPDATE SET
                 recovery=excluded.recovery, base_score=excluded.base_score,
                 z_total=excluded.z_total, contributions=excluded.contributions,
                 flags=excluded.flags, penalty=excluded.penalty,
                 provisional=excluded.provisional, computed_at=excluded.computed_at""",
            (night, score["score"], score["base_score"], score["z_total"],
             json.dumps(score["contributions"]), json.dumps(score["flags"]),
             score["penalty"], 1 if score["provisional"] else 0, time.time()))
        _upsert_sleep_score(conn, history, row, baselines)


def recompute(affected: set) -> int:
    """Recompute derived metrics for the given nights, then rebuild baselines +
    scores from the earliest of them forward. Returns the count recomputed."""
    if not affected:
        return 0
    with db.connect() as conn:
        for night in sorted(affected):
            _upsert_night_metrics(conn, night)
        _rebuild_baselines_and_scores(conn, min(affected))
    return len(affected)


def recompute_all() -> int:
    """Recompute every night from scratch — for retuning weights against stored
    raw data. Returns the number of nights processed."""
    with db.connect() as conn:
        nights = [r["night"] for r in conn.execute(
            """SELECT night FROM health_rr
               UNION SELECT night FROM health_sleep_stages
               UNION SELECT night FROM health_sleep_hr
               UNION SELECT night FROM health_night_samples
               ORDER BY night""").fetchall()]
        for night in nights:
            _upsert_night_metrics(conn, night)
        if nights:
            _rebuild_baselines_and_scores(conn, nights[0])
    return len(nights)


# ---------------------------------------------------------------- endpoints

@bp.post("/ingest")
def ingest():
    """Health Auto Export push. Stores raw rows only; all-or-nothing (a
    malformed payload stores none of it). Response reports per-category
    stored counts, exact-duplicate rows skipped, and ignored metric names."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    data = body.get("data", body)
    if not isinstance(data, dict) or not isinstance(data.get("metrics"), list):
        return jsonify({"error": "expected {'data': {'metrics': [...]}}"}), 400

    stored = {"rr_intervals": 0, "sleep_stages": 0, "sleep_hr": 0,
              "resp_rate": 0, "spo2": 0, "wrist_temp": 0}
    duplicates = 0
    ignored = []
    affected: set = set()
    try:
        with db.connect() as conn:  # one transaction — BadPayload rolls it all back
            for metric in data["metrics"]:
                if not isinstance(metric, dict):
                    raise BadPayload("each metric must be an object")
                name = str(metric.get("name", "")).strip().lower()
                if name == "sleep_analysis":
                    n, d = _ingest_sleep_stages(conn, metric, affected)
                    stored["sleep_stages"] += n
                elif name == "heart_rate":
                    n, d = _ingest_heart_rate(conn, metric, affected)
                    stored["sleep_hr"] += n
                elif name in SAMPLE_METRICS:
                    ours = SAMPLE_METRICS[name]
                    n, d = _ingest_night_samples(conn, metric, ours, affected)
                    stored[ours] += n
                elif name in RR_METRIC_NAMES:
                    n, d = _ingest_rr(conn, metric, affected)
                    stored["rr_intervals"] += n
                else:
                    ignored.append(name or "<unnamed>")
                    continue
                duplicates += d
    except BadPayload as exc:
        log.warning("Health ingest rejected: %s", exc)
        return jsonify({"error": str(exc)}), 400

    # Raw is safely committed; derive metrics/baselines/scores for the touched
    # nights. A compute failure must not lose the raw rows, so it's caught and
    # logged loudly rather than 500-ing the ingest.
    recomputed = []
    try:
        recompute(affected)
        recomputed = sorted(affected)
    except Exception:  # noqa: BLE001 — never lose raw data over a compute bug
        log.exception("Health post-ingest recompute failed for nights %s", sorted(affected))

    log.info("Health ingest: %s (%d duplicates, ignored: %s, recomputed %d nights)",
             stored, duplicates, ignored or "none", len(recomputed))
    return jsonify({"stored": stored, "duplicates": duplicates,
                    "ignored": ignored, "recomputed": recomputed})


@bp.get("/latest-night")
def latest_night():
    """Raw values for the most recent night across all health tables — the
    Health tab's read-only ingest-confirmation list. {"night": null} until
    the first ingest. Counts + windows for the high-volume series (RR, HR),
    full lists for stages and nightly vitals samples (capped defensively)."""
    with db.connect() as conn:
        nights = [row[0] for row in (
            conn.execute("SELECT MAX(night) FROM health_rr").fetchone(),
            conn.execute("SELECT MAX(night) FROM health_sleep_stages").fetchone(),
            conn.execute("SELECT MAX(night) FROM health_sleep_hr").fetchone(),
            conn.execute("SELECT MAX(night) FROM health_night_samples").fetchone(),
        ) if row[0] is not None]
        if not nights:
            return jsonify({"night": None})
        night = max(nights)

        rr = conn.execute(
            """SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
               FROM health_rr WHERE night = ?""", (night,)).fetchone()
        hr = conn.execute(
            """SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                      MIN(bpm) AS min_bpm, MAX(bpm) AS max_bpm
               FROM health_sleep_hr WHERE night = ?""", (night,)).fetchone()
        stages = conn.execute(
            """SELECT stage, start_ts, end_ts FROM health_sleep_stages
               WHERE night = ? ORDER BY start_ts LIMIT 200""", (night,)).fetchall()
        samples = conn.execute(
            """SELECT metric, ts, value FROM health_night_samples
               WHERE night = ? ORDER BY metric, ts LIMIT 500""", (night,)).fetchall()

    by_metric = {"resp_rate": [], "spo2": [], "wrist_temp": []}
    for row in samples:
        by_metric.setdefault(row["metric"], []).append(
            {"ts": row["ts"], "value": row["value"]})
    return jsonify({
        "night": night,
        "rr": {"count": rr["n"], "first_ts": rr["first_ts"], "last_ts": rr["last_ts"]},
        "sleep_hr": {"count": hr["n"], "first_ts": hr["first_ts"], "last_ts": hr["last_ts"],
                     "min_bpm": hr["min_bpm"], "max_bpm": hr["max_bpm"]},
        "sleep_stages": [{"stage": r["stage"], "start": r["start_ts"], "end": r["end_ts"]}
                         for r in stages],
        "samples": by_metric,
    })


def _latest_metrics_night(conn) -> str | None:
    row = conn.execute("SELECT MAX(night) AS n FROM health_night_metrics").fetchone()
    return row["n"]


@bp.get("/night")
def night_readout():
    """Consolidated derived readout for one night: per-night metrics, the
    rolling baselines they were scored against, and the recovery score with
    its full breakdown. ?night=YYYY-MM-DD, default the latest computed night.
    {"night": null} before anything has been computed. The UI (pass 7) reads
    this; passes 2-4 expose it for tests and manual inspection."""
    requested = request.args.get("night")
    with db.connect() as conn:
        night = requested or _latest_metrics_night(conn)
        if night is None:
            return jsonify({"night": None})
        metrics = conn.execute(
            "SELECT * FROM health_night_metrics WHERE night = ?", (night,)).fetchone()
        if metrics is None:
            return jsonify({"night": night, "metrics": None, "baselines": {}, "score": None})
        baseline_rows = conn.execute(
            "SELECT * FROM health_baselines WHERE night = ?", (night,)).fetchall()
        score = conn.execute(
            "SELECT * FROM health_scores WHERE night = ?", (night,)).fetchone()
        sleep = conn.execute(
            "SELECT * FROM health_sleep_scores WHERE night = ?", (night,)).fetchone()
        stage_rows = conn.execute(
            """SELECT stage, start_ts, end_ts FROM health_sleep_stages
               WHERE night = ? ORDER BY start_ts LIMIT 400""", (night,)).fetchall()
        subj = conn.execute(
            "SELECT rating, note FROM health_subjective WHERE night = ?", (night,)).fetchone()

    baselines = {r["metric"]: {
        "mean": r["mean"], "sd": r["sd"], "trend_7": r["trend_7"],
        "swc_low": r["swc_low"], "swc_high": r["swc_high"], "cv": r["cv"],
        "n": r["n"], "provisional": bool(r["provisional"]),
    } for r in baseline_rows}

    return jsonify({
        "night": night,
        "metrics": {k: metrics[k] for k in metrics.keys()},
        "baselines": baselines,
        "score": None if score is None else {
            "recovery": score["recovery"],
            "base_score": score["base_score"],
            "z_total": score["z_total"],
            "contributions": json.loads(score["contributions"]),
            "flags": json.loads(score["flags"]),
            "penalty": score["penalty"],
            "provisional": bool(score["provisional"]),
        },
        "sleep": None if sleep is None else {
            "sleep_score": sleep["sleep_score"],
            "subscores": json.loads(sleep["subscores"]),
            "recovery_index": json.loads(sleep["recovery_index"]) if sleep["recovery_index"] else None,
            "consistency_src": sleep["consistency_src"],
            "restorative_pct": sleep["restorative_pct"],
            "sleep_debt_min": sleep["sleep_debt_min"],
            "target_sleep_min": sleep["target_sleep_min"],
            "sri": sleep["sri"],
            "provisional": bool(sleep["provisional"]),
        },
        "stages": [{"stage": r["stage"], "start": r["start_ts"], "end": r["end_ts"]}
                   for r in stage_rows],
        "subjective": None if subj is None else {"rating": subj["rating"], "note": subj["note"]},
    })


_HISTORY_RANGE_RE = re.compile(r"^(\d+)d$")


@bp.get("/history")
def history():
    """Per-night recovery + sleep scores and driver metrics over a window, for
    the trend charts and the sleep/vitals detail dialogs: nightly vitals
    (rmssd/rhr/resp_rate/spo2/wrist_temp), stage minutes (rem/deep/waso),
    onset/wake timestamps (consistency chart) and the deep-dive sleep values.
    ?range=7d/30d/60d (days, default 30d)."""
    m = _HISTORY_RANGE_RE.match(request.args.get("range", "30d").strip())
    days = min(max(int(m.group(1)), 1), 366) if m else 30
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT me.night AS night, me.rmssd, me.rhr, me.resp_rate,
                      me.spo2, me.wrist_temp, me.tst_min, me.waso_min,
                      me.rem_min, me.deep_min, me.onset_ts, me.wake_ts,
                      sc.recovery, sc.provisional AS rec_provisional,
                      sl.sleep_score, sl.provisional AS sleep_provisional,
                      sl.sri, sl.restorative_pct, sl.sleep_debt_min,
                      sl.target_sleep_min
               FROM health_night_metrics me
               LEFT JOIN health_scores sc ON sc.night = me.night
               LEFT JOIN health_sleep_scores sl ON sl.night = me.night
               WHERE me.night >= ? ORDER BY me.night""", (since,)).fetchall()
    return jsonify({"range": f"{days}d", "nights": [{
        "night": r["night"], "recovery": r["recovery"], "sleep_score": r["sleep_score"],
        "rmssd": r["rmssd"], "rhr": r["rhr"], "resp_rate": r["resp_rate"],
        "spo2": r["spo2"], "wrist_temp": r["wrist_temp"],
        "tst_min": r["tst_min"], "waso_min": r["waso_min"],
        "rem_min": r["rem_min"], "deep_min": r["deep_min"],
        "onset_ts": r["onset_ts"], "wake_ts": r["wake_ts"],
        "sri": r["sri"], "restorative_pct": r["restorative_pct"],
        "sleep_debt_min": r["sleep_debt_min"], "target_sleep_min": r["target_sleep_min"],
        "rec_provisional": bool(r["rec_provisional"]) if r["rec_provisional"] is not None else None,
        "sleep_provisional": bool(r["sleep_provisional"]) if r["sleep_provisional"] is not None else None,
    } for r in rows]})


def morning_snapshot(now: float | None = None) -> dict | None:
    """Last night's recovery + sleep scores, embedded by scenes.py in the
    Sleeping->Day morning summary (mirrors planner.morning_snapshot). Uses the
    night just woken into, or the most recent computed night before it. None
    when there's no health data yet, so summaries stay clean."""
    target = night_of(now if now is not None else time.time())
    with db.connect() as conn:
        row = conn.execute(
            """SELECT night FROM health_night_metrics WHERE night <= ?
               ORDER BY night DESC LIMIT 1""", (target,)).fetchone()
        if row is None:
            return None
        night = row["night"]
        score = conn.execute(
            "SELECT recovery, flags, provisional FROM health_scores WHERE night = ?",
            (night,)).fetchone()
        sleep = conn.execute(
            """SELECT sleep_score, provisional, target_sleep_min, sleep_debt_min
               FROM health_sleep_scores WHERE night = ?""", (night,)).fetchone()
    out = {"night": night}
    if score is not None:
        out["recovery"] = {"score": score["recovery"], "flags": json.loads(score["flags"]),
                           "provisional": bool(score["provisional"])}
    if sleep is not None:
        out["sleep"] = {"score": sleep["sleep_score"], "provisional": bool(sleep["provisional"]),
                        "target_sleep_min": sleep["target_sleep_min"],
                        "sleep_debt_min": sleep["sleep_debt_min"]}
    return out if ("recovery" in out or "sleep" in out) else None


@bp.get("/settings")
def get_settings():
    """Health settings the user owns. `sleep_need_min` drives the sleep score's
    Duration component and pass 6's sleep-debt / target-sleep."""
    return jsonify({"sleep_need_min": _sleep_need()})


@bp.put("/settings")
def put_settings():
    """Update health settings. Body: {"sleep_need_min": <minutes>}. Recomputes
    all sleep scores against the new need (raw is untouched)."""
    body = request.get_json(silent=True) or {}
    if "sleep_need_min" in body:
        try:
            need = float(body["sleep_need_min"])
        except (TypeError, ValueError):
            return jsonify({"error": "sleep_need_min must be a number of minutes"}), 400
        if not 60 <= need <= 900:
            return jsonify({"error": "sleep_need_min must be between 60 and 900"}), 400
        db.set_setting("health_sleep_need_min", need)
        recompute_all()
    return jsonify({"sleep_need_min": _sleep_need()})


@bp.post("/subjective")
def set_subjective():
    """Log the morning's subjective 1-5 recovery feel (pass 8). Body:
    {"rating": 1-5, "night"?: "YYYY-MM-DD", "note"?}. Defaults to the night
    just woken from. Upsert — re-rating a night replaces it. Never touches the
    computed scores; it's the ground truth they get correlated against."""
    body = request.get_json(silent=True) or {}
    rating = body.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
        return jsonify({"error": "rating must be an integer 1-5"}), 400
    night = body.get("night") or night_of(time.time())
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        return jsonify({"error": "note must be a string or null"}), 400
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO health_subjective (night, rating, note, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(night) DO UPDATE SET
                 rating=excluded.rating, note=excluded.note, created_at=excluded.created_at""",
            (night, rating, (note or "").strip() or None, time.time()))
    return jsonify({"night": night, "rating": rating, "note": note})


@bp.get("/correlation")
def correlation():
    """Pearson r between the subjective morning rating and each computed score,
    over every rated night (pass 8). This is the only real way to tune the
    weights to your own feel (§3): a weak/negative r says the model isn't
    tracking how you actually feel. `r` is null until >= 3 rated nights."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT sub.rating, sc.recovery, sl.sleep_score
               FROM health_subjective sub
               LEFT JOIN health_scores sc ON sc.night = sub.night
               LEFT JOIN health_sleep_scores sl ON sl.night = sub.night""").fetchall()
    rec_pairs = [(r["rating"], r["recovery"]) for r in rows]
    slp_pairs = [(r["rating"], r["sleep_score"]) for r in rows]
    return jsonify({
        "ratings": len(rows),
        "recovery": {"r": hc.pearson(rec_pairs),
                     "n": sum(1 for r in rows if r["recovery"] is not None)},
        "sleep": {"r": hc.pearson(slp_pairs),
                  "n": sum(1 for r in rows if r["sleep_score"] is not None)},
    })


@bp.post("/recompute")
def recompute_endpoint():
    """Recompute derived metrics/baselines/scores from stored raw. Body/query
    ?night=YYYY-MM-DD recomputes that night forward; omit it to rebuild every
    night (use after changing weights). Raw rows are never touched."""
    night = request.args.get("night") or (request.get_json(silent=True) or {}).get("night")
    if night:
        count = recompute({night})
    else:
        count = recompute_all()
    return jsonify({"recomputed_nights": count})
