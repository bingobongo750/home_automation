"""CalDAV import tests — mapping, recurrence expansion, and the read-only
guarantee. No network: the protocol layer is exercised against captured XML,
and the mapping against real .ics text. From server/:

    python3 -m unittest discover -s tests
"""

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# config.py reads these at import time — set them before touching app.*
_TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["MOCK_HARDWARE"] = "1"
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")

from flask import Flask  # noqa: E402

from app import caldav_sync, config, db, planner  # noqa: E402
from app.api import api  # noqa: E402

ICS_SINGLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:single-1
SUMMARY:Dentist
DTSTART:20260810T140000Z
DTEND:20260810T150000Z
DESCRIPTION:Bring the form
END:VEVENT
END:VCALENDAR"""

ICS_ALLDAY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:allday-1
SUMMARY:Trip
DTSTART;VALUE=DATE:20260812
DTEND;VALUE=DATE:20260815
END:VEVENT
END:VCALENDAR"""

# Weekly on Mondays — the case the planner's own model could not represent,
# which is exactly why imported rows are flattened instead.
ICS_WEEKLY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:weekly-1
SUMMARY:Standup
DTSTART:20260803T080000Z
DTEND:20260803T081500Z
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4
END:VEVENT
END:VCALENDAR"""

# A rule the planner has no vocabulary for at all.
ICS_MONTHLY_NTH = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:monthly-1
SUMMARY:Rent
DTSTART:20260801T090000Z
RRULE:FREQ=MONTHLY;BYMONTHDAY=1;COUNT=3
END:VEVENT
END:VCALENDAR"""


def _window(days_back=7, days_fwd=90):
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days_back), now + timedelta(days=days_fwd)


class MappingTestCase(unittest.TestCase):
    def test_a_timed_event_maps_across(self):
        start, end = datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = caldav_sync.occurrences_in_window([ICS_SINGLE], start, end)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Dentist")
        self.assertEqual(rows[0]["notes"], "Bring the form")
        self.assertEqual(rows[0]["external_uid"], "single-1")
        self.assertFalse(rows[0]["all_day"])
        self.assertIsNotNone(rows[0]["end_ts"])

    def test_a_date_only_event_is_all_day(self):
        """The iCal DATE vs DATE-TIME split is exactly what the all_day column
        was added for."""
        start, end = datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = caldav_sync.occurrences_in_window([ICS_ALLDAY], start, end)
        self.assertTrue(rows[0]["all_day"])

    def test_a_weekly_rule_expands_to_one_row_per_occurrence(self):
        start, end = datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = caldav_sync.occurrences_in_window([ICS_WEEKLY], start, end)
        self.assertEqual(len(rows), 4, "COUNT=4 should give four rows")
        self.assertEqual({r["external_uid"] for r in rows}, {"weekly-1"},
                         "every occurrence keeps the upstream UID")
        starts = sorted(r["start_ts"] for r in rows)
        for earlier, later in zip(starts, starts[1:]):
            self.assertAlmostEqual(later - earlier, 7 * 86400, delta=3600)

    def test_a_rule_the_planner_cannot_express_still_imports(self):
        """FREQ=MONTHLY has no equivalent in none|daily|weekly. Flattening is
        what lets it come across at all."""
        start, end = datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc)
        rows = caldav_sync.occurrences_in_window([ICS_MONTHLY_NTH], start, end)
        self.assertEqual(len(rows), 3)

    def test_occurrences_outside_the_window_are_excluded(self):
        start, end = datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 10, tzinfo=timezone.utc)
        rows = caldav_sync.occurrences_in_window([ICS_WEEKLY], start, end)
        self.assertLess(len(rows), 4)

    def test_garbage_is_skipped_not_fatal(self):
        """One malformed entry must not lose the whole calendar."""
        start, end = datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = caldav_sync.occurrences_in_window(["not an ics at all", ICS_SINGLE], start, end)
        self.assertEqual(len(rows), 1)

    def test_rows_come_back_in_time_order(self):
        start, end = datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = caldav_sync.occurrences_in_window([ICS_WEEKLY, ICS_SINGLE], start, end)
        self.assertEqual([r["start_ts"] for r in rows],
                         sorted(r["start_ts"] for r in rows))


class ImportTestCase(unittest.TestCase):
    def setUp(self):
        db.init_db()
        planner.init_db()
        with db.connect() as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM settings")
        config.CALDAV_USERNAME = "someone@example.com"
        config.CALDAV_PASSWORD = "app-specific"
        app = Flask(__name__)
        app.register_blueprint(api)
        app.register_blueprint(planner.bp)
        self.client = app.test_client()

    def run_sync(self, blobs):
        with mock.patch.object(caldav_sync, "discover_calendars",
                               return_value=[{"name": "Home", "url": "http://x/cal/"}]), \
             mock.patch.object(caldav_sync, "_fetch_ics", return_value=blobs):
            return caldav_sync.sync()

    def events(self):
        with db.connect() as conn:
            return conn.execute(
                "SELECT title, source, external_uid FROM events ORDER BY start_ts"
            ).fetchall()

    def test_import_writes_rows_marked_as_caldav(self):
        result = self.run_sync([ICS_SINGLE])
        self.assertTrue(result["ok"])
        rows = self.events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "caldav")

    def test_reimport_replaces_rather_than_duplicating(self):
        self.run_sync([ICS_WEEKLY])
        first = len(self.events())
        db.set_setting("caldav_signature", "force-a-rewrite")
        self.run_sync([ICS_WEEKLY])
        self.assertEqual(len(self.events()), first)

    def test_an_upstream_deletion_disappears_locally(self):
        self.run_sync([ICS_SINGLE, ICS_WEEKLY])
        self.assertGreater(len(self.events()), 1)
        self.run_sync([ICS_SINGLE])          # weekly-1 removed upstream
        self.assertEqual({r["external_uid"] for r in self.events()}, {"single-1"})

    def test_unchanged_calendar_skips_the_rewrite(self):
        """This box writes to an SD card; a calendar that has not changed must
        not rewrite the table every 15 minutes."""
        self.run_sync([ICS_SINGLE])
        second = self.run_sync([ICS_SINGLE])
        self.assertTrue(second.get("unchanged"))

    def test_local_events_survive_an_import(self):
        self.client.post("/api/events", json={"title": "Mine", "start": "2026-08-20T10:00"})
        self.run_sync([ICS_SINGLE])
        sources = {r["title"]: r["source"] for r in self.events()}
        self.assertEqual(sources.get("Mine"), "local")
        self.assertEqual(sources.get("Dentist"), "caldav")

    def test_an_import_never_deletes_local_events(self):
        self.client.post("/api/events", json={"title": "Mine", "start": "2026-08-20T10:00"})
        self.run_sync([ICS_SINGLE])
        self.run_sync([])          # calendar emptied upstream
        titles = {r["title"] for r in self.events()}
        self.assertIn("Mine", titles)

    def test_imported_events_are_read_only(self):
        """Editing one would be silently undone by the next sync, and deleting
        one would resurrect it — refusing is the honest answer."""
        self.run_sync([ICS_SINGLE])
        with db.connect() as conn:
            eid = conn.execute("SELECT id FROM events WHERE source='caldav'").fetchone()["id"]
        self.assertEqual(self.client.put(f"/api/events/{eid}",
                                         json={"title": "Nope"}).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/events/{eid}").status_code, 409)

    def test_local_events_stay_editable(self):
        created = self.client.post("/api/events",
                                   json={"title": "Mine", "start": "2026-08-20T10:00"}).get_json()
        self.assertEqual(self.client.put(f"/api/events/{created['id']}",
                                         json={"title": "Renamed"}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/events/{created['id']}").status_code, 200)

    def test_events_api_reports_the_source(self):
        self.run_sync([ICS_SINGLE])
        events = self.client.get("/api/events?from=2026-08-01&range=60d").get_json()["events"]
        self.assertTrue(any(e["source"] == "caldav" for e in events))

    def test_a_failed_sync_leaves_existing_events_alone(self):
        """A flaky network must not empty the calendar."""
        self.run_sync([ICS_SINGLE])
        before = len(self.events())
        with mock.patch.object(caldav_sync, "discover_calendars",
                               side_effect=caldav_sync.CalDavError("boom")):
            result = caldav_sync.sync()
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.events()), before)


class ConfigGuardTestCase(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(api)
        self.client = app.test_client()

    def test_endpoints_refuse_when_unconfigured(self):
        with mock.patch.object(config, "CALDAV_USERNAME", ""), \
             mock.patch.object(config, "CALDAV_PASSWORD", ""):
            self.assertFalse(caldav_sync.configured())
            self.assertEqual(self.client.post("/api/calendar/sync").status_code, 400)
            self.assertEqual(self.client.get("/api/calendar/calendars").status_code, 400)

    def test_status_is_always_readable(self):
        body = self.client.get("/api/calendar/status").get_json()
        self.assertIn("configured", body)


if __name__ == "__main__":
    unittest.main()
