"""Planner: local calendar events + to-do list.

A self-contained module — its own tables (`events`, `tasks`, created by
init_db() here, not in db.SCHEMA), its own blueprint under /api/events and
/api/tasks. Nothing in the device/scene lanes depends on planner data; the
single outward touch is scenes.py calling morning_snapshot() for today's
events and attention-worthy tasks when it builds the Sleeping->Day summary.

Conventions:
- Event start/end are unix epoch seconds, like every other timestamp in the
  app. Write endpoints also accept local "YYYY-MM-DD[THH:MM]" strings (what
  datetime-local / date inputs produce) and convert. `end` is nullable — a
  timed event with no end has no duration.
- all_day is an explicit flag (the iCal DATE-vs-DATE-TIME split, so a CalDAV
  layer maps cleanly): start is floored to local midnight, end — when set —
  is ceiled to the EXCLUSIVE local midnight after the last day; end NULL
  means a single day. Rows from before the flag existed (midnight start, no
  end, previously displayed as all-day by convention) are migrated once.
- Task due dates are plain "YYYY-MM-DD" strings — calendar data, no
  timezone or time-of-day to get wrong; string comparison is date order.
- recurrence is 'none' | 'daily' | 'weekly' — deliberately not RFC 5545.
  Occurrences are expanded on read and keep local wall-clock time across
  DST changes (naive local datetime + timedelta, then .timestamp()).
- category is one of CATEGORIES (or NULL) — a fixed, predefined set the
  dashboard's calendar colors; not user-defined tags.
- external_uid is unused but reserved on both tables so a future CalDAV
  sync layer (e.g. Radicale) can map its UIDs onto rows without a schema
  rewrite.
"""

import logging
import re
import time
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

from . import db

log = logging.getLogger("planner")

bp = Blueprint("planner", __name__, url_prefix="/api")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    start_ts     REAL NOT NULL,          -- unix epoch seconds
    end_ts       REAL,                   -- NULL = no end time / all-day
    notes        TEXT,
    recurrence   TEXT NOT NULL DEFAULT 'none',  -- none | daily | weekly
    category     TEXT,                   -- one of CATEGORIES, or NULL
    all_day      INTEGER NOT NULL DEFAULT 0,  -- 1: start/end are midnight bounds, end exclusive
    external_uid TEXT,                   -- reserved for a future CalDAV sync layer
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events (start_ts);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    due_date     TEXT,                   -- "YYYY-MM-DD" or NULL
    priority     TEXT NOT NULL DEFAULT 'medium',  -- low | medium | high
    done         INTEGER NOT NULL DEFAULT 0,
    list         TEXT,                   -- optional grouping tag ("home", "work")
    created_at   REAL NOT NULL,
    completed_at REAL,
    external_uid TEXT                    -- reserved for a future CalDAV sync layer
);
CREATE INDEX IF NOT EXISTS idx_tasks_done_due ON tasks (done, due_date);
"""

RECURRENCES = ("none", "daily", "weekly")
PRIORITIES = ("low", "medium", "high")
# Predefined event categories — the dashboard's calendar colors each one.
# Keep in sync with the CATEGORIES list in dashboard/app.js.
CATEGORIES = ("home", "work", "personal", "health", "social")

_RECURRENCE_STEPS = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1)}
_PRIORITY_ORDER_SQL = "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"

_RANGE_RE = re.compile(r"^(\d+)d$")
_MAX_OCCURRENCES = 500  # hard stop for one event's expansion in a window

# Only events that can intersect [win_start, win_end) — a recurring series
# repeats forever, so only its start bounds it.
_EVENTS_IN_WINDOW_SQL = """
    SELECT * FROM events
    WHERE start_ts < ?
      AND (recurrence != 'none' OR COALESCE(end_ts, start_ts) >= ?)
    ORDER BY start_ts
"""


def init_db() -> None:
    """Create the planner tables. Called from create_app() right after
    db.init_db() (and from the test suites) — planner owns its own DDL."""
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        # migrations: columns added after the first schema revision
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "category" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN category TEXT")
        if "all_day" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN all_day INTEGER NOT NULL DEFAULT 0")
            # grandfather the pre-flag convention: a midnight start with no
            # end used to be displayed as an all-day entry
            for row in conn.execute(
                "SELECT id, start_ts FROM events WHERE end_ts IS NULL"
            ).fetchall():
                dt = datetime.fromtimestamp(row["start_ts"])
                if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
                    conn.execute("UPDATE events SET all_day = 1 WHERE id = ?", (row["id"],))


# ------------------------------------------------------------------ helpers

def _parse_when(value, field: str) -> float:
    """Epoch seconds from a number or a local ISO string like
    "2026-07-05T15:00" (seconds optional, date-only means midnight)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip()).timestamp()
        except ValueError:
            pass
    raise ValueError(f"{field} must be epoch seconds or a local datetime like 2026-07-05T15:00")


def _local_midnight(day: date) -> float:
    return datetime(day.year, day.month, day.day).timestamp()


def _floor_to_midnight(ts: float) -> float:
    d = datetime.fromtimestamp(ts)
    return datetime(d.year, d.month, d.day).timestamp()


def _normalize_all_day(start_ts: float, end_ts: float | None) -> tuple[float, float | None]:
    """Canonical all-day bounds: start floored to local midnight; end (when
    set) floored to local midnight and kept as the EXCLUSIVE day-after-last
    boundary (iCal DTEND semantics). A same-or-earlier end collapses to a
    single day (end None)."""
    start_ts = _floor_to_midnight(start_ts)
    if end_ts is None:
        return start_ts, None
    end_ts = _floor_to_midnight(end_ts)
    return (start_ts, end_ts) if end_ts > start_ts else (start_ts, None)


def _occurrence_json(row, start: float, end: float | None) -> dict:
    """One occurrence of an event. `start`/`end` are the occurrence's own
    times; series_start/series_end stay the stored row values so the
    dashboard's edit form can prefill the series definition."""
    return {
        "id": row["id"],
        "title": row["title"],
        "start": start,
        "end": end,
        "notes": row["notes"],
        "recurrence": row["recurrence"],
        "category": row["category"],
        "all_day": bool(row["all_day"]),
        "series_start": row["start_ts"],
        "series_end": row["end_ts"],
        "external_uid": row["external_uid"],
    }


def _event_row_json(row) -> dict:
    return _occurrence_json(row, row["start_ts"], row["end_ts"])


def _expand(row, win_start: float, win_end: float) -> list[dict]:
    """Occurrences of one event row intersecting [win_start, win_end).
    Recurrence steps are applied to the naive local datetime, so a 09:00
    event stays at 09:00 across a DST change. All-day events span whole days
    (end exclusive); their length is measured in days, not raw seconds, so a
    multi-day span survives DST intact too."""
    step = _RECURRENCE_STEPS.get(row["recurrence"])
    start_dt = datetime.fromtimestamp(row["start_ts"])
    all_day = bool(row["all_day"])
    if all_day:
        day_span = ((datetime.fromtimestamp(row["end_ts"]).date() - start_dt.date()).days
                    if row["end_ts"] is not None else 0)  # 0 -> single day (end None)
        duration = None
    else:
        duration = (datetime.fromtimestamp(row["end_ts"]) - start_dt) if row["end_ts"] is not None else None

    dt = start_dt
    if step is not None and row["start_ts"] < win_start:
        # jump close to the window instead of stepping from a possibly
        # ancient series start; one step early absorbs any DST-hour skew
        skip = max(int((win_start - row["start_ts"]) // step.total_seconds()) - 1, 0)
        dt += skip * step

    occurrences = []
    for _ in range(_MAX_OCCURRENCES):
        occ_start = dt.timestamp()
        if occ_start >= win_end:
            break
        # Coverage ends (occ_end / end of the covered day) are EXCLUSIVE, so an
        # occurrence ending exactly at win_start belongs to the previous
        # window, not this one. A point event (timed, no end) has no coverage
        # to compare — it's in the window when its instant is at/after start.
        if all_day:
            occ_end = (dt + timedelta(days=day_span)).timestamp() if day_span else None
            span_end = occ_end if occ_end is not None else occ_start + 86400  # covers its one day
            included = span_end > win_start
        else:
            occ_end = (dt + duration).timestamp() if duration is not None else None
            included = occ_end > win_start if occ_end is not None else occ_start >= win_start
        if included:
            occurrences.append(_occurrence_json(row, occ_start, occ_end))
        if step is None:
            break
        dt += step
    return occurrences


def _clean_event(body: dict, *, partial: bool) -> tuple[dict | None, str | None]:
    """Validated column dict from a request body, or (None, error).
    `partial` (PUT) only touches fields present in the body."""
    fields = {}
    if "title" in body or not partial:
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            return None, "title must be a non-empty string"
        fields["title"] = title.strip()
    if "start" in body or not partial:
        if body.get("start") is None:
            return None, "start is required"
        try:
            fields["start_ts"] = _parse_when(body["start"], "start")
        except ValueError as exc:
            return None, str(exc)
    if "end" in body:
        if body["end"] is None:
            fields["end_ts"] = None
        else:
            try:
                fields["end_ts"] = _parse_when(body["end"], "end")
            except ValueError as exc:
                return None, str(exc)
    if "notes" in body:
        notes = body["notes"]
        if notes is not None and not isinstance(notes, str):
            return None, "notes must be a string or null"
        fields["notes"] = (notes or "").strip() or None
    if "recurrence" in body or not partial:
        recurrence = body.get("recurrence") or "none"
        if recurrence not in RECURRENCES:
            return None, f"recurrence must be one of {list(RECURRENCES)}"
        fields["recurrence"] = recurrence
    if "category" in body:
        category = body["category"]
        if category is not None and category not in CATEGORIES:
            return None, f"category must be one of {list(CATEGORIES)} or null"
        fields["category"] = category
    if "all_day" in body or not partial:
        all_day = body.get("all_day", False)
        if not isinstance(all_day, bool):
            return None, "all_day must be a boolean"
        fields["all_day"] = 1 if all_day else 0
    return fields, None


def _clean_task(body: dict, *, partial: bool) -> tuple[dict | None, str | None]:
    fields = {}
    if "title" in body or not partial:
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            return None, "title must be a non-empty string"
        fields["title"] = title.strip()
    if "due_date" in body:
        due = body["due_date"]
        if due is None:
            fields["due_date"] = None
        else:
            try:
                # normalize (zero-padded) so string comparison is date order
                fields["due_date"] = date.fromisoformat(str(due)).isoformat()
            except (TypeError, ValueError):
                return None, "due_date must be YYYY-MM-DD or null"
    if "priority" in body or not partial:
        priority = body.get("priority") or "medium"
        if priority not in PRIORITIES:
            return None, f"priority must be one of {list(PRIORITIES)}"
        fields["priority"] = priority
    if "list" in body:
        tag = body["list"]
        if tag is not None and not isinstance(tag, str):
            return None, "list must be a string or null"
        fields["list"] = (tag or "").strip() or None
    return fields, None


def _task_json(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "due_date": row["due_date"],
        "priority": row["priority"],
        "done": bool(row["done"]),
        "list": row["list"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "external_uid": row["external_uid"],
    }


def _get_event(event_id: int):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def _get_task(task_id: int):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


# ------------------------------------------------------------------- events

@bp.get("/events")
def events_list():
    """Events in a date window, recurring ones expanded into occurrences.
    ?from=YYYY-MM-DD (default today) + ?range=7d/30d/... (days, default 7).
    The window runs from local midnight of `from` for `range` days."""
    raw_from = request.args.get("from")
    if raw_from:
        try:
            day = date.fromisoformat(raw_from)
        except ValueError:
            return jsonify({"error": "from must be YYYY-MM-DD"}), 400
    else:
        day = date.today()
    m = _RANGE_RE.match(request.args.get("range", "7d").strip())
    if not m:
        return jsonify({"error": "range must be a number of days like 7d or 30d"}), 400
    days = min(max(int(m.group(1)), 1), 366)

    win_start = _local_midnight(day)
    win_end = _local_midnight(day + timedelta(days=days))
    with db.connect() as conn:
        rows = conn.execute(_EVENTS_IN_WINDOW_SQL, (win_end, win_start)).fetchall()
    events = [occ for row in rows for occ in _expand(row, win_start, win_end)]
    events.sort(key=lambda e: e["start"])
    return jsonify({"from": win_start, "to": win_end, "events": events})


@bp.post("/events")
def events_create():
    """Body: {"title", "start", "end"?, "notes"?, "recurrence"?, "category"?,
    "all_day"?}. start/end accept epoch seconds or a local ISO string
    ("YYYY-MM-DDTHH:MM", or "YYYY-MM-DD" for all-day). all_day snaps the
    bounds to whole local days (end exclusive)."""
    fields, error = _clean_event(request.get_json(silent=True) or {}, partial=False)
    if error:
        return jsonify({"error": error}), 400
    if fields.get("all_day"):
        fields["start_ts"], fields["end_ts"] = _normalize_all_day(
            fields["start_ts"], fields.get("end_ts"))
    elif fields.get("end_ts") is not None and fields["end_ts"] <= fields["start_ts"]:
        return jsonify({"error": "end must be after start"}), 400
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO events (title, start_ts, end_ts, notes, recurrence, category, all_day, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (fields["title"], fields["start_ts"], fields.get("end_ts"),
             fields.get("notes"), fields["recurrence"], fields.get("category"),
             fields["all_day"], time.time()),
        )
        event_id = cur.lastrowid
    log.info("Event %d created: %r", event_id, fields["title"])
    return jsonify(_event_row_json(_get_event(event_id))), 201


@bp.put("/events/<int:event_id>")
def events_update(event_id: int):
    """Partial update — any subset of title/start/end/notes/recurrence/
    category/all_day. "end": null clears the end time. Toggling all_day
    re-snaps the (possibly unchanged) bounds to whole days or back. Editing a
    recurring event edits the whole series (occurrences are derived, not
    stored)."""
    row = _get_event(event_id)
    if row is None:
        return jsonify({"error": "no such event"}), 404
    fields, error = _clean_event(request.get_json(silent=True) or {}, partial=True)
    if error:
        return jsonify({"error": error}), 400
    new_start = fields.get("start_ts", row["start_ts"])
    new_end = fields["end_ts"] if "end_ts" in fields else row["end_ts"]
    all_day = fields["all_day"] if "all_day" in fields else row["all_day"]
    if all_day:
        # re-snap whenever all_day is on — start/end may be unchanged but the
        # flag could have just been turned on for an existing timed event
        new_start, new_end = _normalize_all_day(new_start, new_end)
        fields["start_ts"], fields["end_ts"] = new_start, new_end
    elif new_end is not None and new_end <= new_start:
        return jsonify({"error": "end must be after start"}), 400
    if fields:
        assignments = ", ".join(f"{col} = ?" for col in fields)
        with db.connect() as conn:
            conn.execute(f"UPDATE events SET {assignments} WHERE id = ?",
                         (*fields.values(), event_id))
    return jsonify(_event_row_json(_get_event(event_id)))


@bp.delete("/events/<int:event_id>")
def events_delete(event_id: int):
    if _get_event(event_id) is None:
        return jsonify({"error": "no such event"}), 404
    with db.connect() as conn:
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    log.info("Event %d deleted", event_id)
    return jsonify({"deleted": event_id})


# -------------------------------------------------------------------- tasks

@bp.get("/tasks")
def tasks_list():
    """Task list, filterable: ?list=home (exact tag), ?done=true/false.
    Sorted open-first, then due date (no due date last), priority, age."""
    conditions, params = [], []
    tag = request.args.get("list")
    if tag:
        conditions.append("list = ?")
        params.append(tag)
    done_raw = request.args.get("done")
    if done_raw is not None:
        done = {"true": 1, "1": 1, "false": 0, "0": 0}.get(done_raw.strip().lower())
        if done is None:
            return jsonify({"error": "done must be true or false"}), 400
        conditions.append("done = ?")
        params.append(done)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM tasks {where}
                ORDER BY done, (due_date IS NULL), due_date,
                         {_PRIORITY_ORDER_SQL}, created_at""",
            params,
        ).fetchall()
    return jsonify({"tasks": [_task_json(r) for r in rows]})


@bp.post("/tasks")
def tasks_create():
    """Body: {"title", "due_date"?, "priority"?, "list"?}."""
    fields, error = _clean_task(request.get_json(silent=True) or {}, partial=False)
    if error:
        return jsonify({"error": error}), 400
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (title, due_date, priority, list, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (fields["title"], fields.get("due_date"), fields["priority"],
             fields.get("list"), time.time()),
        )
        task_id = cur.lastrowid
    log.info("Task %d created: %r", task_id, fields["title"])
    return jsonify(_task_json(_get_task(task_id))), 201


@bp.put("/tasks/<int:task_id>")
def tasks_update(task_id: int):
    """Partial update — any subset of title/due_date/priority/list/done.
    Flipping done maintains completed_at (set on done, cleared on undone)."""
    row = _get_task(task_id)
    if row is None:
        return jsonify({"error": "no such task"}), 404
    body = request.get_json(silent=True) or {}
    fields, error = _clean_task(body, partial=True)
    if error:
        return jsonify({"error": error}), 400
    if "done" in body:
        if not isinstance(body["done"], bool):
            return jsonify({"error": "done must be a boolean"}), 400
        fields["done"] = 1 if body["done"] else 0
        if body["done"]:
            if not row["done"]:
                fields["completed_at"] = time.time()
        else:
            fields["completed_at"] = None
    if fields:
        assignments = ", ".join(f"{col} = ?" for col in fields)
        with db.connect() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?",
                         (*fields.values(), task_id))
    return jsonify(_task_json(_get_task(task_id)))


@bp.post("/tasks/<int:task_id>/complete")
def tasks_complete(task_id: int):
    """One-tap complete. Idempotent — completing a done task changes nothing."""
    row = _get_task(task_id)
    if row is None:
        return jsonify({"error": "no such task"}), 404
    if not row["done"]:
        with db.connect() as conn:
            conn.execute("UPDATE tasks SET done = 1, completed_at = ? WHERE id = ?",
                         (time.time(), task_id))
    return jsonify(_task_json(_get_task(task_id)))


@bp.delete("/tasks/<int:task_id>")
def tasks_delete(task_id: int):
    if _get_task(task_id) is None:
        return jsonify({"error": "no such task"}), 404
    with db.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    log.info("Task %d deleted", task_id)
    return jsonify({"deleted": task_id})


# --------------------------------------------------------- morning snapshot

def morning_snapshot(now: float | None = None) -> dict:
    """Today's calendar + attention-worthy tasks, embedded by scenes.py in
    the Sleeping->Day morning summary. "Today" is the local day containing
    `now` — the day being woken into. Capped so the stored summary stays
    small: 10 events, 10 tasks (open AND overdue-or-high-priority)."""
    now = now if now is not None else time.time()
    today = date.fromtimestamp(now)
    win_start = _local_midnight(today)
    win_end = _local_midnight(today + timedelta(days=1))
    with db.connect() as conn:
        event_rows = conn.execute(_EVENTS_IN_WINDOW_SQL, (win_end, win_start)).fetchall()
        task_rows = conn.execute(
            f"""SELECT * FROM tasks
                WHERE done = 0
                  AND ((due_date IS NOT NULL AND due_date < ?) OR priority = 'high')
                ORDER BY (CASE WHEN due_date IS NOT NULL AND due_date < ? THEN 0 ELSE 1 END),
                         (due_date IS NULL), due_date, {_PRIORITY_ORDER_SQL}
                LIMIT 10""",
            (today.isoformat(), today.isoformat()),
        ).fetchall()

    events = [occ for row in event_rows for occ in _expand(row, win_start, win_end)]
    events.sort(key=lambda e: e["start"])
    return {
        "events": [{"title": e["title"], "start": e["start"], "end": e["end"],
                    "all_day": e["all_day"]}
                   for e in events[:10]],
        "tasks": [{"id": r["id"], "title": r["title"], "due_date": r["due_date"],
                   "priority": r["priority"],
                   "overdue": bool(r["due_date"] and r["due_date"] < today.isoformat())}
                  for r in task_rows],
    }
