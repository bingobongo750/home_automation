"""Uni course tracker tests — throwaway DB, no hardware, no threads. From the
server/ directory:

    python3 -m unittest discover -s tests

Covers: the seeded semester and today's week, course CRUD + validation,
marking slots done/skipped/open, and progress counts (skipped slots leave
the total; marks outside a shrunk course are hidden, not lost).
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

# config.py reads these at import time — set them before touching app.*
_TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["MOCK_HARDWARE"] = "1"
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from flask import Flask  # noqa: E402

from app import db, uni  # noqa: E402


class UniTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        uni.init_db()
        app = Flask(__name__)
        app.register_blueprint(uni.bp)
        cls.client = app.test_client()

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM uni_marks")
            conn.execute("DELETE FROM uni_courses")
            conn.execute("UPDATE uni_semester SET weeks = 14, start_date = ?",
                         (uni._monday(date.today()).isoformat(),))

    def add_course(self, **body):
        body.setdefault("name", "Signals")
        resp = self.client.post("/api/uni/courses", json=body)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()["courses"][-1]

    def mark(self, course_id, week, kind, idx, status):
        return self.client.put(f"/api/uni/courses/{course_id}/marks",
                               json={"week": week, "kind": kind, "idx": idx, "status": status})

    # ------------------------------------------------------------ semester

    def test_semester_snaps_to_monday_and_reports_current_week(self):
        three_weeks_ago_wed = uni._monday(date.today()) - timedelta(weeks=3) + timedelta(days=2)
        resp = self.client.put("/api/uni/semester",
                               json={"name": "Autumn 2026", "start": three_weeks_ago_wed.isoformat()})
        sem = resp.get_json()["semester"]
        self.assertEqual(sem["name"], "Autumn 2026")
        self.assertEqual(date.fromisoformat(sem["start"]).weekday(), 0)
        self.assertEqual(sem["current_week"], 4)

    def test_semester_validation(self):
        self.assertEqual(self.client.put("/api/uni/semester", json={"weeks": 0}).status_code, 400)
        self.assertEqual(self.client.put("/api/uni/semester", json={"start": "nope"}).status_code, 400)
        self.assertEqual(self.client.put("/api/uni/semester", json={"name": " "}).status_code, 400)

    # ------------------------------------------------------------- courses

    def test_course_defaults_and_color_rotation(self):
        first = self.add_course(name="A")
        second = self.add_course(name="B")
        self.assertEqual((first["lectures"], first["exercises"]), (1, 1))
        self.assertEqual(first["color"], uni.COLORS[0])
        self.assertEqual(second["color"], uni.COLORS[1])
        self.assertEqual(first["total"], 14 * 2)

    def test_course_validation(self):
        post = lambda body: self.client.post("/api/uni/courses", json=body).status_code
        self.assertEqual(post({"name": ""}), 400)
        self.assertEqual(post({"name": "X", "lectures": 9}), 400)
        self.assertEqual(post({"name": "X", "exercises": True}), 400)
        self.assertEqual(post({"name": "X", "color": "pink"}), 400)

    def test_update_and_delete_course(self):
        course = self.add_course()
        resp = self.client.put(f"/api/uni/courses/{course['id']}",
                               json={"name": "Power electronics", "exercises": 2})
        updated = resp.get_json()["courses"][0]
        self.assertEqual((updated["name"], updated["exercises"]), ("Power electronics", 2))
        self.mark(course["id"], 1, "lecture", 0, "done")
        resp = self.client.delete(f"/api/uni/courses/{course['id']}")
        self.assertEqual(resp.get_json()["courses"], [])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM uni_marks").fetchone()[0], 0)
        self.assertEqual(self.client.delete(f"/api/uni/courses/{course['id']}").status_code, 404)

    # --------------------------------------------------------------- marks

    def test_done_skipped_and_reopen(self):
        course = self.add_course(lectures=2, exercises=1)   # 14 * 3 = 42 slots
        self.mark(course["id"], 1, "lecture", 0, "done")
        self.mark(course["id"], 1, "lecture", 1, "done")
        resp = self.mark(course["id"], 1, "exercise", 0, "skipped")
        c = resp.get_json()["courses"][0]
        self.assertEqual((c["done"], c["skipped"], c["total"]), (2, 1, 41))

        # re-marking overwrites; open deletes the row
        self.mark(course["id"], 1, "exercise", 0, "done")
        c = self.mark(course["id"], 1, "lecture", 0, "open").get_json()["courses"][0]
        self.assertEqual((c["done"], c["skipped"], c["total"]), (2, 0, 42))
        self.assertEqual(len(c["marks"]), 2)

    def test_mark_validation(self):
        course = self.add_course(lectures=1, exercises=0)
        self.assertEqual(self.mark(course["id"], 15, "lecture", 0, "done").status_code, 400)
        self.assertEqual(self.mark(course["id"], 1, "lecture", 1, "done").status_code, 400)
        self.assertEqual(self.mark(course["id"], 1, "exercise", 0, "done").status_code, 400)
        self.assertEqual(self.mark(course["id"], 1, "quiz", 0, "done").status_code, 400)
        self.assertEqual(self.mark(course["id"], 1, "lecture", 0, "maybe").status_code, 400)
        self.assertEqual(self.mark(999, 1, "lecture", 0, "done").status_code, 404)

    def test_shrinking_hides_marks_without_losing_them(self):
        course = self.add_course(lectures=2, exercises=1)
        self.mark(course["id"], 1, "lecture", 1, "done")
        self.mark(course["id"], 14, "exercise", 0, "done")
        cid = course["id"]

        c = self.client.put(f"/api/uni/courses/{cid}", json={"lectures": 1}).get_json()["courses"][0]
        self.client.put("/api/uni/semester", json={"weeks": 12})
        c = self.client.get("/api/uni").get_json()["courses"][0]
        self.assertEqual((c["done"], c["marks"]), (0, []))

        self.client.put(f"/api/uni/courses/{cid}", json={"lectures": 2})
        self.client.put("/api/uni/semester", json={"weeks": 14})
        c = self.client.get("/api/uni").get_json()["courses"][0]
        self.assertEqual(c["done"], 2)


if __name__ == "__main__":
    unittest.main()
