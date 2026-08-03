"""Presence tests — the state machine, the arrival time rule, and the away
summary's trimming and clustering. Runs against MOCK_HARDWARE=1, no threads
waiting on real time. From server/:

    python3 -m unittest discover -s tests
"""

import os
import tempfile
import time
import unittest

# config.py reads these at import time — set them before touching app.*
_TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["MOCK_HARDWARE"] = "1"
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")

from flask import Flask  # noqa: E402

from app import config, db, health, lighting, planner, poller, presence, scenes  # noqa: E402
from app.api import api  # noqa: E402
from app.mystrom import make_plug  # noqa: E402
from app.shelly_bulb import make_bulb  # noqa: E402

_BUILT = False


def _reset():
    """Same shape as test_scenes: build the mock device clients create_app
    would have built, then clear state between tests."""
    global _BUILT
    if not _BUILT:
        db.init_db()
        planner.init_db()   # the morning summary queries these
        health.init_db()
        for device in db.list_devices():
            if device["type"] == "wifi_plug":
                poller.plugs[device["id"]] = make_plug(device["ip"])
            elif device["type"] == "bulb_zone":
                lighting.zones[device["id"]] = make_bulb(device["ip"])
        _BUILT = True
    with db.connect() as conn:
        conn.execute("DELETE FROM readings")
        conn.execute("DELETE FROM power_readings")
        conn.execute("DELETE FROM settings")
    with scenes._lock:
        scenes._cancel_wake_locked()
        scenes._cancel_bedtime_locked()
    presence._cancel_depart_locked()


class ArrivalSceneTestCase(unittest.TestCase):
    """§5 — Day or Sleeping, from the stored nightly window."""

    def setUp(self):
        _reset()

    def at(self, hhmm):
        h, m = (int(x) for x in hhmm.split(":"))
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))

    def test_default_window_midnight_to_0930(self):
        db.set_sleep_schedule({"enabled": True, "sleep_time": "00:00", "wake_time": "09:30"})
        self.assertEqual(presence.scene_for_arrival(self.at("02:00"))[0], "Sleeping")
        self.assertEqual(presence.scene_for_arrival(self.at("09:29"))[0], "Sleeping")
        self.assertEqual(presence.scene_for_arrival(self.at("09:30"))[0], "Home")
        self.assertEqual(presence.scene_for_arrival(self.at("18:00"))[0], "Home")

    def test_window_wrapping_past_midnight(self):
        db.set_sleep_schedule({"enabled": True, "sleep_time": "23:00", "wake_time": "07:00"})
        self.assertEqual(presence.scene_for_arrival(self.at("23:30"))[0], "Sleeping")
        self.assertEqual(presence.scene_for_arrival(self.at("03:00"))[0], "Sleeping")
        self.assertEqual(presence.scene_for_arrival(self.at("06:59"))[0], "Sleeping")
        self.assertEqual(presence.scene_for_arrival(self.at("07:00"))[0], "Home")
        self.assertEqual(presence.scene_for_arrival(self.at("22:59"))[0], "Home")

    def test_sleeping_arrival_carries_the_schedules_wake_time(self):
        """Getting home at 02:00 must not cost the morning summary."""
        db.set_sleep_schedule({"enabled": True, "sleep_time": "00:00", "wake_time": "09:30"})
        scene, wake = presence.scene_for_arrival(self.at("02:00"))
        self.assertEqual((scene, wake), ("Sleeping", "09:30"))

    def test_disabled_schedule_is_always_day(self):
        db.set_sleep_schedule({"enabled": False, "sleep_time": "00:00", "wake_time": "09:30"})
        self.assertEqual(presence.scene_for_arrival(self.at("02:00"))[0], "Home")

    def test_zero_length_window_is_always_day(self):
        db.set_sleep_schedule({"enabled": True, "sleep_time": "03:00", "wake_time": "03:00"})
        self.assertEqual(presence.scene_for_arrival(self.at("03:00"))[0], "Home")

    def test_malformed_times_fall_back_to_day(self):
        db.set_sleep_schedule({"enabled": True, "sleep_time": "nonsense", "wake_time": "09:30"})
        self.assertEqual(presence.scene_for_arrival(self.at("02:00"))[0], "Home")


class StateMachineTestCase(unittest.TestCase):
    """§4 — the case table, driven through the HTTP endpoints."""

    def setUp(self):
        _reset()
        config.PRESENCE_DEPART_GRACE_S = 0.05   # keep the tests quick
        app = Flask(__name__)
        app.register_blueprint(api)
        self.client = app.test_client()
        self.addCleanup(presence._cancel_depart_locked)

    def settle(self):
        deadline = time.time() + 3
        while time.time() < deadline and presence.state()["departure_pending"]:
            time.sleep(0.02)
        time.sleep(0.05)

    def scene(self):
        active = db.get_active_scene()
        return active["name"] if active else "Home"

    def test_departure_applies_away_only_after_the_grace_period(self):
        r = self.client.post("/api/presence/departed").get_json()
        self.assertFalse(r["applied"])
        self.assertNotEqual(self.scene(), "Away")   # not yet
        self.settle()
        self.assertEqual(self.scene(), "Away")
        self.assertEqual(presence.state()["state"], "away")

    def test_arrival_during_grace_cancels_silently(self):
        """The bounce case: nothing was applied, so nothing to undo."""
        self.client.post("/api/presence/departed")
        r = self.client.post("/api/presence/arrived").get_json()
        self.assertFalse(r["applied"])
        self.settle()
        self.assertNotEqual(self.scene(), "Away")

    def test_second_departure_does_not_restart_the_countdown(self):
        self.client.post("/api/presence/departed")
        r = self.client.post("/api/presence/departed").get_json()
        self.assertFalse(r["applied"])
        self.assertIn("pending", r["reason"])
        self.settle()
        self.assertEqual(self.scene(), "Away")

    def test_departure_while_already_away_keeps_the_original_since(self):
        self.client.post("/api/presence/departed")
        self.settle()
        first_since = presence.state()["since"]
        r = self.client.post("/api/presence/departed").get_json()
        self.assertFalse(r["applied"])
        self.assertEqual(presence.state()["since"], first_since)

    def test_departure_from_sleeping_still_goes_away(self):
        scenes.activate("Sleeping", "09:30")
        self.client.post("/api/presence/departed")
        self.settle()
        self.assertEqual(self.scene(), "Away")
        self.assertIsNone(db.get_active_scene()["wake_at"])  # pending wake cancelled

    def test_arrival_while_in_day_is_a_no_op(self):
        scenes.activate("Home")
        r = self.client.post("/api/presence/arrived").get_json()
        self.assertFalse(r["applied"])
        self.assertEqual(self.scene(), "Home")

    def test_arrival_while_in_sleeping_is_a_no_op(self):
        """Protects a scene the user chose by hand from a stray geofence."""
        scenes.activate("Sleeping", "09:30")
        r = self.client.post("/api/presence/arrived").get_json()
        self.assertFalse(r["applied"])
        self.assertEqual(self.scene(), "Sleeping")

    def test_arrival_from_away_restores_a_scene(self):
        self.client.post("/api/presence/departed")
        self.settle()
        r = self.client.post("/api/presence/arrived").get_json()
        self.assertTrue(r["applied"])
        self.assertIn(r["scene"], ("Home", "Sleeping"))
        self.assertEqual(presence.state()["state"], "home")

    def test_get_presence_reports_pending_departure(self):
        self.client.post("/api/presence/departed")
        self.assertTrue(self.client.get("/api/presence").get_json()["departure_pending"])
        self.settle()
        self.assertFalse(self.client.get("/api/presence").get_json()["departure_pending"])


class BedtimeGuardTestCase(unittest.TestCase):
    """§6 — Away must survive the nightly schedule."""

    def setUp(self):
        _reset()

    def test_bedtime_timer_skips_an_away_house(self):
        db.set_sleep_schedule({"enabled": True, "sleep_time": "00:00", "wake_time": "09:30"})
        scenes.activate("Away")
        with scenes._lock:
            generation = scenes._bedtime_generation
        scenes._fire_bedtime(generation)
        self.assertEqual(db.get_active_scene()["name"], "Away",
                         "the nightly schedule must not put an empty flat to Sleeping")

    def test_bedtime_timer_still_fires_when_someone_is_home(self):
        db.set_sleep_schedule({"enabled": True, "sleep_time": "00:00", "wake_time": "09:30"})
        scenes.activate("Home")
        with scenes._lock:
            generation = scenes._bedtime_generation
        scenes._fire_bedtime(generation)
        self.assertEqual(db.get_active_scene()["name"], "Sleeping")


class ClusteringTestCase(unittest.TestCase):
    """§8 — repeated detections are one event, not many."""

    def test_a_burst_of_samples_is_one_event(self):
        ts = [1000 + 5 * i for i in range(40)]   # 40 samples, 5s apart
        self.assertEqual(len(scenes.cluster_events(ts, 300)), 1)

    def test_a_long_gap_starts_a_new_event(self):
        events = scenes.cluster_events([1000, 1005, 1010, 5000, 5005], 300)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["samples"], 3)
        self.assertEqual(events[1]["samples"], 2)

    def test_no_detections_is_no_events(self):
        self.assertEqual(scenes.cluster_events([], 300), [])

    def test_unsorted_input_is_handled(self):
        self.assertEqual(len(scenes.cluster_events([5000, 1000, 1005], 300)), 2)


class AwaySummaryTestCase(unittest.TestCase):
    """§7 — content, the arrival trim, and the minimum duration."""

    def setUp(self):
        _reset()

    def test_quiet_window_is_not_disturbed(self):
        now = time.time()
        for i in range(20):
            db.insert_reading("motion", 0, ts=now - 3000 + i * 60)
            db.insert_reading("temp", 21.0, ts=now - 3000 + i * 60)
        s = scenes._compute_away_summary(now - 3600, now)
        self.assertFalse(s["disturbed"])
        self.assertEqual(s["motion"]["events"], 0)

    def test_motion_makes_it_disturbed_and_is_clustered(self):
        now = time.time()
        for i in range(30):   # one burst, 5s apart
            db.insert_reading("motion", 1, ts=now - 3000 + i * 5)
        s = scenes._compute_away_summary(now - 3600, now)
        self.assertTrue(s["disturbed"])
        self.assertEqual(s["motion"]["events"], 1, "one crossing is one event")
        self.assertEqual(s["motion"]["samples"], 30)

    def test_co2_is_omitted_rather_than_zeroed_when_the_sensor_is_dead(self):
        """A broken sensor must never read as 'all clear'."""
        now = time.time()
        s = scenes._compute_away_summary(now - 3600, now)
        self.assertIsNone(s["co2"])

    def test_a_co2_rise_alone_counts_as_disturbed(self):
        """A person sitting still defeats the PIR but not CO2."""
        now = time.time()
        db.insert_reading("co2", 500, ts=now - 3500)
        db.insert_reading("co2", 900, ts=now - 100)
        s = scenes._compute_away_summary(now - 3600, now)
        self.assertTrue(s["co2"]["rose_significantly"])
        self.assertTrue(s["disturbed"])


class ArrivalTrimTestCase(unittest.TestCase):
    """The run-up to a detected arrival is you, not an intruder."""

    def setUp(self):
        _reset()
        config.PRESENCE_DEPART_GRACE_S = 0.05
        app = Flask(__name__)
        app.register_blueprint(api)
        self.client = app.test_client()
        self.addCleanup(presence._cancel_depart_locked)

    def test_motion_just_before_arrival_is_excluded(self):
        now = time.time()
        db.set_presence("away", now - 7200, "departed", now - 7200)
        db.set_active_scene("Away", now - 7200)
        # you, walking in, inside the trim window
        for i in range(10):
            db.insert_reading("motion", 1, ts=now - 60 + i * 5)

        self.client.post("/api/presence/arrived")
        summary = db.get_setting("last_away_summary")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["motion"]["events"], 0,
                         "walking through your own front door is not a disturbance")
        self.assertFalse(summary["disturbed"])

    def test_short_absence_generates_no_summary(self):
        now = time.time()
        db.set_presence("away", now - 60, "departed", now - 60)
        db.set_active_scene("Away", now - 60)
        r = self.client.post("/api/presence/arrived").get_json()
        self.assertTrue(r["applied"])
        self.assertFalse(r["summary_generated"])
        self.assertIsNone(db.get_setting("last_away_summary"))

    def test_summary_endpoint_is_null_before_the_first(self):
        self.assertIsNone(self.client.get("/api/scenes/last-away-summary")
                          .get_json()["summary"])


class DayToHomeMigrationTestCase(unittest.TestCase):
    """The rename touches stored rows, so it has to migrate, not just reseed."""

    def test_an_existing_day_row_is_renamed_not_duplicated(self):
        with db.connect() as conn:
            conn.execute("UPDATE scenes SET name = 'Day' WHERE name = 'Home'")
        db.init_db()
        with db.connect() as conn:
            names = [r["name"] for r in conn.execute("SELECT name FROM scenes")]
        self.assertIn("Home", names)
        self.assertNotIn("Day", names)
        self.assertEqual(names.count("Home"), 1, "must rename, not insert a twin")

    def test_a_persisted_active_day_scene_is_migrated(self):
        """Otherwise the hub boots pointing at a scene that no longer exists."""
        db.set_active_scene("Day", 1785790000.0)
        db.init_db()
        self.assertEqual(db.get_active_scene()["name"], "Home")


class NightAwakeningsTestCase(unittest.TestCase):
    """The overnight summary counts times you got up, not PIR rows."""

    def setUp(self):
        _reset()

    def test_one_long_burst_is_one_awakening(self):
        now = time.time()
        for i in range(40):     # 40 samples over ~3 min: one trip to the loo
            db.insert_reading("motion", 1, ts=now - 3600 + i * 5)
        s = scenes._compute_sleep_summary(now - 7200, now)
        self.assertEqual(s["motion"]["count"], 1)
        self.assertEqual(s["motion"]["samples"], 40, "raw count is kept alongside")

    def test_separate_trips_count_separately(self):
        now = time.time()
        for base in (now - 6000, now - 3000, now - 900):
            for i in range(10):
                db.insert_reading("motion", 1, ts=base + i * 5)
        s = scenes._compute_sleep_summary(now - 7200, now)
        self.assertEqual(s["motion"]["count"], 3)

    def test_undisturbed_night_is_zero(self):
        now = time.time()
        for i in range(60):
            db.insert_reading("motion", 0, ts=now - 3600 + i * 60)
        s = scenes._compute_sleep_summary(now - 7200, now)
        self.assertEqual(s["motion"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
