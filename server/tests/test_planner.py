"""Planner (calendar + to-do) tests — same harness as test_scenes: mock
hardware, throwaway DB, no threads. From the server/ directory:

    python3 -m unittest discover -s tests

Covers: event CRUD + validation, the date-window query with recurrence
expansion (daily/weekly, wall-clock times), task CRUD + filters + one-tap
complete, and the planner section of the Sleeping->Day morning summary.
"""

import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta

# config.py reads these at import time — set them before touching app.*
_TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["MOCK_HARDWARE"] = "1"
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from flask import Flask  # noqa: E402

from app import db, lighting, planner, poller  # noqa: E402
from app.api import api  # noqa: E402
from app.mystrom import make_plug  # noqa: E402
from app.wled import make_wled_zone  # noqa: E402


def make_client():
    app = Flask(__name__)
    app.register_blueprint(api)
    app.register_blueprint(planner.bp)
    return app.test_client()


def local(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """A naive local datetime `day_offset` days from today at hour:minute."""
    base = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return base + timedelta(days=day_offset)


class PlannerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        planner.init_db()
        # mock device clients so the summary test can activate scenes
        for device in db.list_devices():
            if device["type"] == "wifi_plug":
                poller.plugs[device["id"]] = make_plug(device["ip"])
            elif device["type"] == "wled_zone":
                lighting.zones[device["id"]] = make_wled_zone(device["ip"])
        cls.client = make_client()

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM settings")

    # -------------------------------------------------------------- helpers

    def add_event(self, **body):
        resp = self.client.post("/api/events", json=body)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def add_task(self, **body):
        resp = self.client.post("/api/tasks", json=body)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def events_in(self, query=""):
        resp = self.client.get(f"/api/events{query}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["events"]

    # --------------------------------------------------------------- events

    def test_event_create_accepts_iso_and_epoch(self):
        start = local(1, 15)
        ev = self.add_event(title="Dentist", start=start.strftime("%Y-%m-%dT%H:%M"),
                            end=start.timestamp() + 3600, notes="bring card",
                            category="health")
        self.assertEqual(ev["start"], start.timestamp())
        self.assertEqual(ev["end"], start.timestamp() + 3600)
        self.assertEqual(ev["recurrence"], "none")
        self.assertEqual(ev["notes"], "bring card")
        self.assertEqual(ev["category"], "health")
        self.assertIsNone(ev["external_uid"])  # reserved for CalDAV sync

    def test_event_category_optional_and_validated(self):
        ev = self.add_event(title="Untagged", start=local(1, 10).timestamp())
        self.assertIsNone(ev["category"])
        resp = self.client.post("/api/events", json={
            "title": "X", "start": local(1, 10).timestamp(), "category": "hobbies"})
        self.assertEqual(resp.status_code, 400)
        # clearing via PUT
        tagged = self.add_event(title="Y", start=local(1, 11).timestamp(), category="work")
        resp = self.client.put(f"/api/events/{tagged['id']}", json={"category": None})
        self.assertIsNone(resp.get_json()["category"])

    def test_all_day_single_day_snaps_to_midnight(self):
        # a date-only start (what an all-day date input sends), no end
        ev = self.add_event(title="Holiday", start="2026-07-09", all_day=True)
        self.assertTrue(ev["all_day"])
        self.assertIsNone(ev["end"])
        self.assertEqual(ev["start"], datetime(2026, 7, 9).timestamp())
        # a timed start given with all_day still floors to that day's midnight
        ev2 = self.add_event(title="Off", start="2026-07-09T14:30", all_day=True)
        self.assertEqual(ev2["start"], datetime(2026, 7, 9).timestamp())
        self.assertIsNone(ev2["end"])

    def test_all_day_multi_day_end_is_exclusive(self):
        # covers Jul 9, 10, 11 — end is the exclusive midnight after the last
        ev = self.add_event(title="Trip", start="2026-07-09",
                            end="2026-07-12", all_day=True)
        self.assertTrue(ev["all_day"])
        self.assertEqual(ev["start"], datetime(2026, 7, 9).timestamp())
        self.assertEqual(ev["end"], datetime(2026, 7, 12).timestamp())
        # an end on/before the start day collapses to a single day
        ev2 = self.add_event(title="One day", start="2026-07-09",
                             end="2026-07-09", all_day=True)
        self.assertIsNone(ev2["end"])

    def test_toggle_all_day_on_and_off(self):
        ev = self.add_event(title="Meeting", start="2026-07-09T14:30",
                            end="2026-07-09T15:30")
        self.assertFalse(ev["all_day"])
        # turning all_day on re-snaps the existing bounds to whole days
        on = self.client.put(f"/api/events/{ev['id']}", json={"all_day": True}).get_json()
        self.assertTrue(on["all_day"])
        self.assertEqual(on["start"], datetime(2026, 7, 9).timestamp())
        self.assertIsNone(on["end"])  # within one day -> single all-day
        # turning it back off leaves it a (midnight-anchored) timed event
        off = self.client.put(f"/api/events/{ev['id']}", json={"all_day": False}).get_json()
        self.assertFalse(off["all_day"])

    def test_all_day_validation(self):
        self.assertEqual(self.client.post("/api/events", json={
            "title": "X", "start": "2026-07-09", "all_day": "yes"}).status_code, 400)

    def test_multi_day_timed_event_spans_window(self):
        # a timed event running across three days shows when the window is any
        # of those days (the frontend clips it per day column)
        self.add_event(title="Conference", start=local(0, 9).timestamp(),
                       end=local(2, 17).timestamp())
        # querying only the middle day still returns it
        mid = date.today() + timedelta(days=1)
        got = self.events_in(f"?from={mid.isoformat()}&range=1d")
        self.assertEqual([e["title"] for e in got], ["Conference"])
        self.assertEqual(got[0]["start"], local(0, 9).timestamp())
        self.assertEqual(got[0]["end"], local(2, 17).timestamp())

    def test_all_day_recurrence_spans_days(self):
        # a 2-day all-day event repeating weekly keeps its 2-day span
        self.add_event(title="Market weekend", start="2026-07-09",
                       end="2026-07-11", recurrence="weekly", all_day=True)
        got = self.events_in("?from=2026-07-06&range=21d")
        self.assertEqual(len(got), 3)  # Jul 9, 16, 23
        for occ in got:
            self.assertTrue(occ["all_day"])
            self.assertEqual(occ["end"] - occ["start"], 2 * 86400)

    def test_event_validation(self):
        post = lambda body: self.client.post("/api/events", json=body).status_code
        self.assertEqual(post({"start": time.time()}), 400)               # no title
        self.assertEqual(post({"title": "  ", "start": time.time()}), 400)
        self.assertEqual(post({"title": "X"}), 400)                       # no start
        self.assertEqual(post({"title": "X", "start": "whenever"}), 400)
        self.assertEqual(post({"title": "X", "start": time.time(),
                               "recurrence": "monthly"}), 400)            # not supported
        self.assertEqual(post({"title": "X", "start": time.time(),
                               "end": time.time() - 60}), 400)            # end before start
        self.assertEqual(self.client.get("/api/events?range=7x").status_code, 400)
        self.assertEqual(self.client.get("/api/events?from=someday").status_code, 400)

    def test_window_includes_overlap_excludes_rest(self):
        self.add_event(title="Yesterday", start=local(-1, 10).timestamp())
        self.add_event(title="Next month", start=local(40, 10).timestamp())
        self.add_event(title="Today", start=local(0, 9).timestamp())
        # started before the window but still running into it
        self.add_event(title="Overnight", start=local(-1, 23).timestamp(),
                       end=local(0, 1).timestamp())
        titles = [e["title"] for e in self.events_in()]  # default: 7d from today
        self.assertEqual(titles, ["Overnight", "Today"])

    def test_window_start_boundary_is_exclusive_for_coverage(self):
        # coverage ends are exclusive: an event that ENDS exactly at the
        # window's midnight start belongs to the previous day, not this one
        today = date.today()
        self.add_event(title="Late call", start=local(-1, 22).timestamp(),
                       end=datetime(today.year, today.month, today.day).timestamp())
        self.add_event(title="Weekend away",  # all-day, exclusive end = today 00:00
                       start=(today - timedelta(days=2)).isoformat(),
                       end=today.isoformat(), all_day=True)
        # a point event exactly at the boundary instant IS today's
        self.add_event(title="Midnight ping",
                       start=datetime(today.year, today.month, today.day).timestamp())
        titles = [e["title"] for e in self.events_in(f"?from={today.isoformat()}&range=1d")]
        self.assertEqual(titles, ["Midnight ping"])
        # same rule inside the morning snapshot's "today" list
        snap = planner.morning_snapshot()
        self.assertEqual([e["title"] for e in snap["events"]], ["Midnight ping"])

    def test_daily_recurrence_expands_at_wall_clock_time(self):
        # series started yesterday 09:00 — the next 7 days hold 7 occurrences
        self.add_event(title="Standup", start=local(-1, 9).timestamp(),
                       end=local(-1, 9, 15).timestamp(), recurrence="daily")
        occurrences = self.events_in()
        self.assertEqual(len(occurrences), 7)
        for occ in occurrences:
            start_dt = datetime.fromtimestamp(occ["start"])
            self.assertEqual((start_dt.hour, start_dt.minute), (9, 0))
            self.assertEqual(occ["end"] - occ["start"], 15 * 60)
            self.assertEqual(occ["series_start"], local(-1, 9).timestamp())
        # consecutive local days
        days = [datetime.fromtimestamp(o["start"]).date() for o in occurrences]
        self.assertEqual(days, [date.today() + timedelta(days=i) for i in range(7)])

    def test_weekly_recurrence(self):
        self.add_event(title="Bins", start=local(2, 7).timestamp(), recurrence="weekly")
        self.assertEqual(len(self.events_in()), 1)              # once in 7 days
        self.assertEqual(len(self.events_in("?range=30d")), 4)  # days 2, 9, 16, 23

    def test_event_update_and_delete(self):
        ev = self.add_event(title="Dinner", start=local(1, 19).timestamp(),
                            end=local(1, 21).timestamp())
        resp = self.client.put(f"/api/events/{ev['id']}", json={"title": "Dinner out"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["title"], "Dinner out")
        self.assertEqual(resp.get_json()["end"], ev["end"])  # untouched

        # "end": null clears the end time
        resp = self.client.put(f"/api/events/{ev['id']}", json={"end": None})
        self.assertIsNone(resp.get_json()["end"])
        # merged validation: moving start after the stored end is rejected
        self.client.put(f"/api/events/{ev['id']}", json={"end": local(1, 20).timestamp()})
        resp = self.client.put(f"/api/events/{ev['id']}",
                               json={"start": local(1, 22).timestamp()})
        self.assertEqual(resp.status_code, 400)

        self.assertEqual(self.client.delete(f"/api/events/{ev['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/events/{ev['id']}").status_code, 404)
        self.assertEqual(self.client.put("/api/events/9999", json={}).status_code, 404)

    # ---------------------------------------------------------------- tasks

    def test_task_create_defaults(self):
        task = self.add_task(title="Fix bike")
        self.assertEqual(task["priority"], "medium")
        self.assertFalse(task["done"])
        self.assertIsNone(task["due_date"])
        self.assertIsNone(task["list"])
        self.assertIsNone(task["completed_at"])

    def test_task_validation(self):
        post = lambda body: self.client.post("/api/tasks", json=body).status_code
        self.assertEqual(post({}), 400)
        self.assertEqual(post({"title": "X", "priority": "urgent"}), 400)
        self.assertEqual(post({"title": "X", "due_date": "tomorrow"}), 400)
        self.assertEqual(self.client.get("/api/tasks?done=maybe").status_code, 400)

    def test_task_filters(self):
        self.add_task(title="Bills", list="home")
        self.add_task(title="Report", list="work")
        done = self.add_task(title="Old chore", list="home")
        self.client.post(f"/api/tasks/{done['id']}/complete")

        by_list = self.client.get("/api/tasks?list=home").get_json()["tasks"]
        self.assertEqual({t["title"] for t in by_list}, {"Bills", "Old chore"})
        open_only = self.client.get("/api/tasks?done=false").get_json()["tasks"]
        self.assertEqual({t["title"] for t in open_only}, {"Bills", "Report"})
        both = self.client.get("/api/tasks?list=home&done=true").get_json()["tasks"]
        self.assertEqual([t["title"] for t in both], ["Old chore"])

    def test_task_sort_order(self):
        today = date.today().isoformat()
        self.add_task(title="no due, low", priority="low")
        self.add_task(title="due today, medium", due_date=today)
        self.add_task(title="due today, high", due_date=today, priority="high")
        finished = self.add_task(title="done", due_date=today, priority="high")
        self.client.post(f"/api/tasks/{finished['id']}/complete")

        titles = [t["title"] for t in self.client.get("/api/tasks").get_json()["tasks"]]
        self.assertEqual(titles, ["due today, high", "due today, medium",
                                  "no due, low", "done"])

    def test_complete_and_reopen(self):
        task = self.add_task(title="Water plants")
        done = self.client.post(f"/api/tasks/{task['id']}/complete").get_json()
        self.assertTrue(done["done"])
        self.assertIsNotNone(done["completed_at"])
        # idempotent — completing again keeps the original timestamp
        again = self.client.post(f"/api/tasks/{task['id']}/complete").get_json()
        self.assertEqual(again["completed_at"], done["completed_at"])
        # reopening via PUT clears the completion timestamp
        reopened = self.client.put(f"/api/tasks/{task['id']}",
                                   json={"done": False}).get_json()
        self.assertFalse(reopened["done"])
        self.assertIsNone(reopened["completed_at"])

    def test_task_update_and_delete(self):
        task = self.add_task(title="Call plumber", list="home")
        resp = self.client.put(f"/api/tasks/{task['id']}",
                               json={"priority": "high", "list": None})
        self.assertEqual(resp.get_json()["priority"], "high")
        self.assertIsNone(resp.get_json()["list"])
        self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 404)
        self.assertEqual(self.client.post("/api/tasks/9999/complete").status_code, 404)

    # ------------------------------------------------------ morning summary

    def test_morning_snapshot_contents(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.add_event(title="Dentist", start=local(0, 15).timestamp())
        self.add_event(title="Standup", start=local(-3, 9).timestamp(), recurrence="daily")
        self.add_event(title="Next week", start=local(8, 9).timestamp())
        self.add_task(title="Overdue chore", due_date=yesterday)
        self.add_task(title="Big thing", priority="high")
        self.add_task(title="Whenever", priority="low")           # not attention-worthy
        finished = self.add_task(title="Done overdue", due_date=yesterday)
        self.client.post(f"/api/tasks/{finished['id']}/complete")

        snap = planner.morning_snapshot()
        self.assertEqual([e["title"] for e in snap["events"]], ["Standup", "Dentist"])
        self.assertEqual([t["title"] for t in snap["tasks"]],
                         ["Overdue chore", "Big thing"])
        self.assertTrue(snap["tasks"][0]["overdue"])
        self.assertFalse(snap["tasks"][1]["overdue"])
        self.assertEqual(snap["tasks"][0]["due_date"], yesterday)

    def test_summary_carries_planner_section(self):
        self.add_event(title="Dentist", start=local(0, 15).timestamp())
        self.add_task(title="Overdue chore",
                      due_date=(date.today() - timedelta(days=2)).isoformat())
        db.set_active_scene("Sleeping", time.time() - 8 * 3600)
        resp = self.client.post("/api/scenes/Day/activate", json={})
        self.assertEqual(resp.status_code, 200)

        summary = self.client.get("/api/scenes/last-summary").get_json()["summary"]
        self.assertEqual([e["title"] for e in summary["planner"]["events"]], ["Dentist"])
        self.assertEqual([t["title"] for t in summary["planner"]["tasks"]],
                         ["Overdue chore"])
        # the sensor half of the summary is still there alongside
        self.assertIn("co2", summary)
        self.assertIn("motion", summary)


if __name__ == "__main__":
    unittest.main()
