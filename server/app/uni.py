"""University planner: courses and their weekly lectures/exercises.

A self-contained module, like planner.py — its own tables (created by
init_db() here, not in db.SCHEMA) and its own blueprint under /api/uni.
Nothing in the device/scene lanes reads it, and it does not touch the
calendar: a course here is a weekly checklist, not a set of timed events.

Model:
- One semester (single row): a name, the Monday of week 1, and how many
  weeks it runs. Week numbers everywhere else are 1-based against it.
- A course says how many lectures and exercises it has per week. Every
  (week, kind, idx) slot that implies is "open" until marked otherwise.
- A mark records a slot as `done` or `skipped` (it didn't take place).
  Only non-open slots have a row; marking a slot open deletes its row.
  Marks outside the course's current shape (a week past the semester's
  end, an index past the per-week count) are kept but not counted — so
  shrinking a course and growing it back again loses nothing.
- Progress counts skipped slots out of the total: a lecture that never
  happened is not one you are behind on.
"""

import logging
import time
from datetime import date, timedelta

from flask import Blueprint, jsonify, request

from . import db

log = logging.getLogger("uni")

bp = Blueprint("uni", __name__, url_prefix="/api/uni")

SCHEMA = """
CREATE TABLE IF NOT EXISTS uni_semester (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT NOT NULL,
    start_date  TEXT NOT NULL,          -- "YYYY-MM-DD", always a Monday (week 1)
    weeks       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS uni_courses (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL,          -- one of COLORS
    lectures    INTEGER NOT NULL,       -- per week
    exercises   INTEGER NOT NULL,       -- per week
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS uni_marks (
    course_id   INTEGER NOT NULL,
    week        INTEGER NOT NULL,       -- 1-based semester week
    kind        TEXT NOT NULL,          -- lecture | exercise
    idx         INTEGER NOT NULL,       -- 0-based slot within the week
    status      TEXT NOT NULL,          -- done | skipped (open = no row)
    updated_at  REAL NOT NULL,
    PRIMARY KEY (course_id, week, kind, idx)
);
"""

KINDS = ("lecture", "exercise")
STATUSES = ("open", "done", "skipped")
# Course swatches — the calendar's validated category hues under neutral
# names. Keep in sync with COURSE_COLORS in dashboard/app.js.
COLORS = ("blue", "green", "violet", "amber", "orange")
MAX_PER_WEEK = 5
MAX_WEEKS = 30
DEFAULT_WEEKS = 14


def init_db() -> None:
    """Create the uni tables and seed a semester starting this week, so the
    overview renders before anything is configured."""
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO uni_semester (id, name, start_date, weeks) VALUES (1, ?, ?, ?)",
            ("Semester", _monday(date.today()).isoformat(), DEFAULT_WEEKS),
        )


# ------------------------------------------------------------------ helpers

def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _semester(conn) -> dict:
    row = conn.execute("SELECT * FROM uni_semester WHERE id = 1").fetchone()
    start = date.fromisoformat(row["start_date"])
    # can be < 1 before the semester starts or > weeks after it ends; the
    # dashboard only highlights a week that actually exists
    current = (date.today() - start).days // 7 + 1
    return {"name": row["name"], "start": row["start_date"], "weeks": row["weeks"],
            "current_week": current}


def _course_json(row, marks: list, weeks: int) -> dict:
    slots = {"lecture": row["lectures"], "exercise": row["exercises"]}
    in_shape = [m for m in marks
                if 1 <= m["week"] <= weeks and m["idx"] < slots[m["kind"]]]
    done = sum(1 for m in in_shape if m["status"] == "done")
    skipped = sum(1 for m in in_shape if m["status"] == "skipped")
    return {
        "id": row["id"],
        "name": row["name"],
        "color": row["color"],
        "lectures": row["lectures"],
        "exercises": row["exercises"],
        "marks": in_shape,
        "done": done,
        "skipped": skipped,
        # every slot the semester holds, minus the ones that never happened
        "total": weeks * (row["lectures"] + row["exercises"]) - skipped,
    }


def _overview(conn) -> dict:
    semester = _semester(conn)
    marks_by_course: dict[int, list] = {}
    for m in conn.execute("SELECT * FROM uni_marks ORDER BY week, kind, idx"):
        marks_by_course.setdefault(m["course_id"], []).append(
            {"week": m["week"], "kind": m["kind"], "idx": m["idx"], "status": m["status"]})
    courses = [
        _course_json(row, marks_by_course.get(row["id"], []), semester["weeks"])
        for row in conn.execute("SELECT * FROM uni_courses ORDER BY created_at, id")
    ]
    return {"semester": semester, "courses": courses}


def _int_field(body: dict, key: str, lo: int, hi: int) -> tuple[int | None, str | None]:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return None, f"{key} must be an integer {lo}-{hi}"
    return value, None


def _clean_course(body: dict, *, partial: bool) -> tuple[dict | None, str | None]:
    fields = {}
    if "name" in body or not partial:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return None, "name must be a non-empty string"
        fields["name"] = name.strip()[:120]
    if "color" in body:
        if body["color"] not in COLORS:
            return None, f"color must be one of {list(COLORS)}"
        fields["color"] = body["color"]
    for key in ("lectures", "exercises"):
        if key in body or not partial:
            body.setdefault(key, 1)
            value, error = _int_field(body, key, 0, MAX_PER_WEEK)
            if error:
                return None, error
            fields[key] = value
    return fields, None


def _get_course(conn, course_id: int):
    return conn.execute("SELECT * FROM uni_courses WHERE id = ?", (course_id,)).fetchone()


# ------------------------------------------------------------------ routes

@bp.get("")
def overview():
    """The whole overview in one fetch: the semester (with today's week) and
    every course with its marks and progress counts."""
    with db.connect() as conn:
        return jsonify(_overview(conn))


@bp.put("/semester")
def semester_update():
    """Partial update of {"name", "start", "weeks"}. `start` is any date in
    week 1 and is snapped back to its Monday."""
    body = request.get_json(silent=True) or {}
    fields = {}
    if "name" in body:
        if not isinstance(body["name"], str) or not body["name"].strip():
            return jsonify({"error": "name must be a non-empty string"}), 400
        fields["name"] = body["name"].strip()[:60]
    if "start" in body:
        try:
            fields["start_date"] = _monday(date.fromisoformat(str(body["start"]))).isoformat()
        except ValueError:
            return jsonify({"error": "start must be YYYY-MM-DD"}), 400
    if "weeks" in body:
        weeks, error = _int_field(body, "weeks", 1, MAX_WEEKS)
        if error:
            return jsonify({"error": error}), 400
        fields["weeks"] = weeks
    with db.connect() as conn:
        if fields:
            assignments = ", ".join(f"{col} = ?" for col in fields)
            conn.execute(f"UPDATE uni_semester SET {assignments} WHERE id = 1",
                         tuple(fields.values()))
        return jsonify(_overview(conn))


@bp.post("/courses")
def course_create():
    """Body: {"name", "color"?, "lectures"?, "exercises"?} — per-week counts
    0-5, default 1 each. Without a color the next swatch in rotation is used."""
    fields, error = _clean_course(request.get_json(silent=True) or {}, partial=False)
    if error:
        return jsonify({"error": error}), 400
    with db.connect() as conn:
        if "color" not in fields:
            count = conn.execute("SELECT COUNT(*) FROM uni_courses").fetchone()[0]
            fields["color"] = COLORS[count % len(COLORS)]
        cur = conn.execute(
            """INSERT INTO uni_courses (name, color, lectures, exercises, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (fields["name"], fields["color"], fields["lectures"], fields["exercises"],
             time.time()),
        )
        log.info("Course %d created: %r", cur.lastrowid, fields["name"])
        return jsonify(_overview(conn)), 201


@bp.put("/courses/<int:course_id>")
def course_update(course_id: int):
    """Partial update of the POST fields. Lowering a per-week count hides the
    marks beyond it rather than deleting them."""
    fields, error = _clean_course(request.get_json(silent=True) or {}, partial=True)
    if error:
        return jsonify({"error": error}), 400
    with db.connect() as conn:
        if _get_course(conn, course_id) is None:
            return jsonify({"error": "no such course"}), 404
        if fields:
            assignments = ", ".join(f"{col} = ?" for col in fields)
            conn.execute(f"UPDATE uni_courses SET {assignments} WHERE id = ?",
                         (*fields.values(), course_id))
        return jsonify(_overview(conn))


@bp.delete("/courses/<int:course_id>")
def course_delete(course_id: int):
    with db.connect() as conn:
        if _get_course(conn, course_id) is None:
            return jsonify({"error": "no such course"}), 404
        conn.execute("DELETE FROM uni_marks WHERE course_id = ?", (course_id,))
        conn.execute("DELETE FROM uni_courses WHERE id = ?", (course_id,))
        log.info("Course %d deleted", course_id)
        return jsonify(_overview(conn))


@bp.put("/courses/<int:course_id>/marks")
def mark_set(course_id: int):
    """Body: {"week", "kind", "idx", "status"} — status `done`, `skipped`
    (it didn't take place) or `open` (clears the mark). The slot must exist
    in the course's current shape."""
    body = request.get_json(silent=True) or {}
    kind, status = body.get("kind"), body.get("status")
    if kind not in KINDS:
        return jsonify({"error": f"kind must be one of {list(KINDS)}"}), 400
    if status not in STATUSES:
        return jsonify({"error": f"status must be one of {list(STATUSES)}"}), 400
    with db.connect() as conn:
        course = _get_course(conn, course_id)
        if course is None:
            return jsonify({"error": "no such course"}), 404
        weeks = _semester(conn)["weeks"]
        week, error = _int_field(body, "week", 1, weeks)
        if error:
            return jsonify({"error": error}), 400
        per_week = course["lectures" if kind == "lecture" else "exercises"]
        if per_week == 0:
            return jsonify({"error": f"this course has no {kind}s"}), 400
        idx, error = _int_field(body, "idx", 0, per_week - 1)
        if error:
            return jsonify({"error": error}), 400
        if status == "open":
            conn.execute(
                "DELETE FROM uni_marks WHERE course_id = ? AND week = ? AND kind = ? AND idx = ?",
                (course_id, week, kind, idx))
        else:
            conn.execute(
                """INSERT INTO uni_marks (course_id, week, kind, idx, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (course_id, week, kind, idx)
                   DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at""",
                (course_id, week, kind, idx, status, time.time()))
        return jsonify(_overview(conn))
