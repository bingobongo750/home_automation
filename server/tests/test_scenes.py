"""Scene (house mode) tests — run against MOCK_HARDWARE=1, no threads, no
physical devices. From the server/ directory:

    python3 -m unittest discover -s tests

Covers: seeded scene definitions, activation applying device states,
auto-lighting suppression, the wake-time scheduler (arm, cancel, stale-fire
guard, overdue-at-startup), and the Sleeping->Day overnight summary.
"""

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

# config.py reads these at import time — set them before touching app.*
_TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["MOCK_HARDWARE"] = "1"
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")

from flask import Flask  # noqa: E402

from app import db, health, lighting, planner, poller, scenes  # noqa: E402
from app.api import api  # noqa: E402
from app.mystrom import make_plug  # noqa: E402
from app.shelly_bulb import make_bulb  # noqa: E402


def make_client():
    app = Flask(__name__)
    app.register_blueprint(api)
    return app.test_client()


class SceneTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        planner.init_db()  # the morning summary queries the planner tables
        health.init_db()   # ...and the health tables (last night's scores)
        # What create_app's start() calls do, minus the threads: build mock
        # device clients so scene activation has something to drive.
        for device in db.list_devices():
            if device["type"] == "wifi_plug":
                poller.plugs[device["id"]] = make_plug(device["ip"])
            elif device["type"] == "bulb_zone":
                lighting.zones[device["id"]] = make_bulb(device["ip"])
        cls.client = make_client()
        cls.devices = {d["name"]: d for d in db.list_devices()}

    def setUp(self):
        # Fresh slate: no readings, no active scene, no summary, no lock, and
        # no timer left over from the previous test.
        with db.connect() as conn:
            conn.execute("DELETE FROM readings")
            conn.execute("DELETE FROM settings")
        for device in db.list_devices():
            db.set_device_locked(device["id"], False)
        with scenes._lock:
            scenes._cancel_wake_locked()
            scenes._cancel_bedtime_locked()

    def tearDown(self):
        with scenes._lock:
            scenes._cancel_wake_locked()
            scenes._cancel_bedtime_locked()

    # ------------------------------------------------------------- helpers

    def zone(self, name):
        return lighting.zones[self.devices[name]["id"]]

    def plug(self, name):
        return poller.plugs[self.devices[name]["id"]]

    def activate(self, name, body=None):
        return self.client.post(f"/api/scenes/{name}/activate", json=body or {})

    # ---------------------------------------------------------------- seeds

    def test_scenes_seeded(self):
        resp = self.client.get("/api/scenes")
        self.assertEqual(resp.status_code, 200)
        by_name = {s["name"]: s for s in resp.get_json()}
        self.assertEqual(set(by_name), {"Sleeping", "Day", "Away"})
        # every plug and every zone off while sleeping — group keys, so a
        # future third plug/zone is covered without touching the scene
        sleeping = by_name["Sleeping"]["states"]
        self.assertFalse(sleeping["all_plugs"]["on"])
        self.assertFalse(sleeping["all_zones"]["on"])
        # Day has no zone targets — zones resume their own mode instead
        self.assertTrue(by_name["Day"]["states"]["all_plugs"]["on"])
        self.assertNotIn("all_zones", by_name["Day"]["states"])

    def test_legacy_scene_rows_migrated(self):
        import json
        # a DB seeded by any earlier revision (night light, per-name lists)…
        for legacy in db._LEGACY_SCENE_STATES["Sleeping"]:
            with db.connect() as conn:
                conn.execute("UPDATE scenes SET states = ? WHERE name = 'Sleeping'",
                             (json.dumps(legacy),))
            db.init_db()
            self.assertEqual(db.get_scene("Sleeping")["states"],
                             db.SCENE_SEEDS["Sleeping"])
        # …but a hand-edited row is never clobbered
        edited = {"all_plugs": {"on": False}, "Cupboard": {"on": True, "brightness": 10}}
        with db.connect() as conn:
            conn.execute("UPDATE scenes SET states = ? WHERE name = 'Sleeping'",
                         (json.dumps(edited),))
        db.init_db()
        self.assertEqual(db.get_scene("Sleeping")["states"], edited)
        with db.connect() as conn:  # restore the seed for the other tests
            conn.execute("UPDATE scenes SET states = ? WHERE name = 'Sleeping'",
                         (json.dumps(db.SCENE_SEEDS["Sleeping"]),))

    def test_device_name_overrides_group_target(self):
        import json
        # a hand-tuned scene: everything off, but the cupboard keeps a dim glow
        states = {"all_zones": {"on": False},
                  "Cupboard": {"on": True, "brightness": 10}}
        with db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO scenes (name, states) VALUES ('TestNight', ?)",
                         (json.dumps(states),))
        try:
            resp = self.activate("TestNight")
            self.assertEqual(resp.status_code, 200)
            cupboard = self.zone("Cupboard").state()
            self.assertTrue(cupboard["on"])
            self.assertEqual(cupboard["brightness"], 10)
            self.assertFalse(self.zone("Room LED").state()["on"])
        finally:
            with db.connect() as conn:
                conn.execute("DELETE FROM scenes WHERE name = 'TestNight'")

    # ----------------------------------------------------------- activation

    def test_activate_away_sets_devices_and_suppresses_auto(self):
        self.plug("Plug 1").set_state(True)
        self.plug("Plug 2").set_state(True)
        self.zone("Cupboard").set_state(on=True)
        self.zone("Room LED").set_state(on=True)

        resp = self.activate("Away")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["active"]["name"], "Away")
        self.assertTrue(all(d["ok"] for d in data["devices"]), data["devices"])
        # the group keys resolved to every actual device
        self.assertEqual({d["device"] for d in data["devices"]},
                         {"Plug 1", "Plug 2", "Cupboard", "Room LED"})

        self.assertFalse(self.plug("Plug 1").report()["relay_on"])
        self.assertFalse(self.plug("Plug 2").report()["relay_on"])
        self.assertFalse(self.zone("Cupboard").state()["on"])
        self.assertFalse(self.zone("Room LED").state()["on"])
        self.assertEqual(lighting._suppressing_scene(), "Away")

        active = self.client.get("/api/scenes/active").get_json()
        self.assertEqual(active["name"], "Away")
        self.assertIsNotNone(active["activated_at"])
        self.assertIsNone(active["wake_at"])

    def test_activate_sleeping_states(self):
        self.zone("Cupboard").set_state(on=True, brightness=255)
        self.zone("Room LED").set_state(on=True)
        self.plug("Plug 1").set_state(True)
        self.plug("Plug 2").set_state(True)
        resp = self.activate("Sleeping")
        self.assertEqual(resp.status_code, 200)

        # everything dark: every LED zone off, every (unlocked) plug off
        self.assertFalse(self.zone("Cupboard").state()["on"])
        self.assertFalse(self.zone("Room LED").state()["on"])
        self.assertFalse(self.plug("Plug 1").report()["relay_on"])
        self.assertFalse(self.plug("Plug 2").report()["relay_on"])
        self.assertEqual(lighting._suppressing_scene(), "Sleeping")

    def test_day_lifts_suppression_and_preserves_zone_mode(self):
        room_led_id = self.devices["Room LED"]["id"]
        db.set_device_mode(room_led_id, "auto")

        self.activate("Sleeping")
        # a scene never rewrites the mode column — suppression is global
        self.assertEqual(db.get_device(room_led_id)["mode"], "auto")
        self.assertEqual(lighting._suppressing_scene(), "Sleeping")

        resp = self.activate("Day")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(lighting._suppressing_scene())
        self.assertEqual(db.get_device(room_led_id)["mode"], "auto")
        self.assertTrue(self.plug("Plug 1").report()["relay_on"])

    def test_unknown_scene_404(self):
        self.assertEqual(self.activate("Party").status_code, 404)

    def test_default_active_scene_is_day(self):
        active = self.client.get("/api/scenes/active").get_json()
        self.assertEqual(active["name"], "Day")
        self.assertIsNone(active["activated_at"])
        self.assertIsNone(lighting._suppressing_scene())

    def test_locked_plug_is_skipped(self):
        plug_id = self.devices["Plug 1"]["id"]
        self.plug("Plug 1").set_state(True)
        db.set_device_locked(plug_id, True)

        data = self.activate("Away").get_json()
        plug_result = next(d for d in data["devices"] if d["device"] == "Plug 1")
        self.assertFalse(plug_result["ok"])
        self.assertEqual(plug_result["skipped"], "locked")
        self.assertTrue(self.plug("Plug 1").report()["relay_on"])  # untouched
        # the rest of the scene still applied
        self.assertFalse(self.zone("Room LED").state()["on"])

    # ------------------------------------------------------------ wake time

    def test_wake_time_validation(self):
        self.assertEqual(self.activate("Sleeping", {"wake_time": "25:00"}).status_code, 400)
        self.assertEqual(self.activate("Sleeping", {"wake_time": "7am"}).status_code, 400)
        self.assertEqual(self.activate("Day", {"wake_time": "07:00"}).status_code, 400)
        self.assertEqual(self.activate("Sleeping", {"wake_time": 700}).status_code, 400)
        # blank means "no wake time", not an error
        self.assertEqual(self.activate("Sleeping", {"wake_time": "  "}).status_code, 200)
        self.assertIsNone(self.client.get("/api/scenes/active").get_json()["wake_at"])

    def test_next_wake_at(self):
        now = datetime(2026, 7, 4, 22, 30).timestamp()
        # still ahead today
        self.assertEqual(scenes.next_wake_at("23:15", now),
                         datetime(2026, 7, 4, 23, 15).timestamp())
        # already passed -> tomorrow
        self.assertEqual(scenes.next_wake_at("07:00", now),
                         datetime(2026, 7, 5, 7, 0).timestamp())

    def test_wake_scheduled_and_cancelled_by_manual_change(self):
        resp = self.activate("Sleeping", {"wake_time": "07:00"})
        self.assertEqual(resp.status_code, 200)
        active = resp.get_json()["active"]
        self.assertEqual(active["wake_time"], "07:00")
        self.assertGreater(active["wake_at"], time.time())
        self.assertIsNotNone(scenes._wake_timer)

        # manual change before the wake fires cancels the pending transition
        self.activate("Away")
        self.assertIsNone(scenes._wake_timer)
        self.assertIsNone(self.client.get("/api/scenes/active").get_json()["wake_at"])

    def test_stale_wake_fire_is_noop(self):
        self.activate("Sleeping", {"wake_time": "07:00"})
        stale_generation = scenes._wake_generation
        self.activate("Away")
        # simulate the old timer's callback racing in after the manual change
        scenes._fire_wake(stale_generation)
        self.assertEqual(db.get_active_scene()["name"], "Away")

    def test_wake_fire_transitions_to_day(self):
        self.activate("Sleeping", {"wake_time": "07:00"})
        # invoke the pending timer's callback as if the clock had struck
        scenes._fire_wake(scenes._wake_generation)
        self.assertEqual(db.get_active_scene()["name"], "Day")
        self.assertIsNone(lighting._suppressing_scene())
        self.assertIsNotNone(db.get_setting("last_sleep_summary"))

    def test_overdue_wake_fires_at_startup(self):
        # backend "went down" mid-night and came back after the wake time
        db.set_active_scene("Sleeping", time.time() - 8 * 3600,
                            "07:00", time.time() - 600)
        scenes.init()
        self.assertEqual(db.get_active_scene()["name"], "Day")
        self.assertIsNotNone(db.get_setting("last_sleep_summary"))

    def test_future_wake_rearmed_at_startup(self):
        db.set_active_scene("Sleeping", time.time() - 3600,
                            "07:00", time.time() + 3600)
        scenes.init()
        self.assertIsNotNone(scenes._wake_timer)
        self.assertEqual(db.get_active_scene()["name"], "Sleeping")

    # ------------------------------------------------------ nightly schedule

    def test_sleep_schedule_defaults(self):
        data = self.client.get("/api/settings/sleep-schedule").get_json()
        self.assertEqual(data, {"enabled": True, "sleep_time": "00:00",
                                "wake_time": "09:30"})

    def test_sleep_schedule_validation(self):
        for bad in ({"sleep_time": "24:00"}, {"wake_time": "9.30"},
                    {"sleep_time": None}, {"wake_time": 930}):
            body = {"enabled": True, "sleep_time": "00:00", "wake_time": "09:30", **bad}
            self.assertEqual(
                self.client.put("/api/settings/sleep-schedule", json=body).status_code,
                400, bad)

    def test_sleep_schedule_saved_and_armed(self):
        resp = self.client.put("/api/settings/sleep-schedule",
                               json={"enabled": True, "sleep_time": "23:15",
                                     "wake_time": "06:45"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.get_sleep_schedule()["sleep_time"], "23:15")
        self.assertIsNotNone(scenes._bedtime_timer)

    def test_disabled_schedule_is_not_armed(self):
        self.client.put("/api/settings/sleep-schedule",
                        json={"enabled": False, "sleep_time": "00:00",
                              "wake_time": "09:30"})
        self.assertIsNone(scenes._bedtime_timer)

    def test_bedtime_fire_activates_sleeping_with_wake(self):
        scenes.reschedule_bedtime()
        # invoke the pending timer's callback as if midnight had struck
        scenes._fire_bedtime(scenes._bedtime_generation)
        active = db.get_active_scene()
        self.assertEqual(active["name"], "Sleeping")
        self.assertEqual(active["wake_time"], "09:30")
        self.assertEqual(lighting._suppressing_scene(), "Sleeping")
        # the morning end of the window is armed by the usual wake machinery
        self.assertIsNotNone(scenes._wake_timer)
        # ...and tonight's timer has already come round for tomorrow
        self.assertIsNotNone(scenes._bedtime_timer)

    def test_nightly_schedule_leaves_a_locked_plug_alone(self):
        # the lock is what stops the schedule cutting power to something that
        # must stay on overnight — it has to hold for the automatic Sleeping
        # exactly as it does for a manual one
        locked_id = self.devices["Plug 2"]["id"]
        self.plug("Plug 1").set_state(True)
        self.plug("Plug 2").set_state(True)
        db.set_device_locked(locked_id, True)

        scenes.reschedule_bedtime()
        scenes._fire_bedtime(scenes._bedtime_generation)

        self.assertEqual(db.get_active_scene()["name"], "Sleeping")
        self.assertTrue(self.plug("Plug 2").report()["relay_on"])   # untouched
        self.assertFalse(self.plug("Plug 1").report()["relay_on"])  # rest applied
        self.assertFalse(self.zone("Cupboard").state()["on"])

    def test_locked_plug_refuses_any_toggle(self):
        # 423 in both directions, not just off — the plug must be unlocked
        # first, which is the only path that touches the lock
        plug_id = self.devices["Plug 1"]["id"]
        db.set_device_locked(plug_id, True)
        for body in ({"on": False}, {"on": True}, {}):
            resp = self.client.post(f"/api/devices/{plug_id}/toggle", json=body)
            self.assertEqual(resp.status_code, 423, body)
            self.assertTrue(resp.get_json()["locked"])

    def test_stale_bedtime_fire_is_noop(self):
        scenes.reschedule_bedtime()
        stale_generation = scenes._bedtime_generation
        self.activate("Away")
        scenes.reschedule_bedtime()  # e.g. the schedule was edited meanwhile
        scenes._fire_bedtime(stale_generation)
        self.assertEqual(db.get_active_scene()["name"], "Away")

    def test_manual_change_does_not_cancel_tomorrows_bedtime(self):
        scenes.reschedule_bedtime()
        # a manual scene change cancels a pending wake, but the recurring
        # schedule has to survive it
        self.activate("Away")
        self.assertIsNone(scenes._wake_timer)
        self.assertIsNotNone(scenes._bedtime_timer)

    def test_reactivating_sleeping_keeps_window_start(self):
        self.activate("Sleeping")
        first = db.get_active_scene()["activated_at"]
        time.sleep(0.02)
        # changing the wake time mid-night must not restart the summary window
        self.activate("Sleeping", {"wake_time": "08:30"})
        active = db.get_active_scene()
        self.assertEqual(active["activated_at"], first)
        self.assertEqual(active["wake_time"], "08:30")

    # ------------------------------------------------------------- summary

    def test_sleep_summary_computation(self):
        start = time.time() - 8 * 3600
        # readings across the sleeping window
        for hours, temp, hum, co2 in [(0, 21.0, 45.0, 500),
                                      (2, 19.5, 50.0, 700),
                                      (4, 18.0, 55.0, 850),
                                      (7, 19.0, 52.0, 900)]:
            ts = start + hours * 3600
            db.insert_reading("temp", temp, ts)
            db.insert_reading("hum", hum, ts)
            db.insert_reading("co2", co2, ts)
        db.insert_reading("motion", 1, start + 3 * 3600)
        db.insert_reading("motion", 0, start + 3.5 * 3600)
        db.insert_reading("motion", 1, start + 5 * 3600)
        # outside the window — must not leak into the summary
        db.insert_reading("temp", 30.0, start - 3600)
        db.insert_reading("motion", 1, start - 3600)

        db.set_active_scene("Sleeping", start)
        self.assertEqual(self.activate("Day").status_code, 200)

        summary = self.client.get("/api/scenes/last-summary").get_json()["summary"]
        self.assertAlmostEqual(summary["from"], start, places=1)
        self.assertEqual(summary["temp"]["min"], 18.0)
        self.assertEqual(summary["temp"]["max"], 21.0)
        self.assertAlmostEqual(summary["temp"]["avg"], 19.4, places=1)
        self.assertEqual(summary["hum"]["min"], 45.0)
        self.assertEqual(summary["hum"]["max"], 55.0)
        self.assertEqual(summary["co2"]["avg"], 738)  # mean of 500/700/850/900
        self.assertEqual(summary["co2"]["start"], 500)
        self.assertEqual(summary["co2"]["end"], 900)
        self.assertEqual(summary["co2"]["delta"], 400)
        self.assertTrue(summary["co2"]["rose_significantly"])
        self.assertEqual(summary["motion"]["count"], 2)
        self.assertEqual(len(summary["motion"]["events"]), 2)

    def test_summary_flat_co2_not_flagged(self):
        start = time.time() - 6 * 3600
        db.insert_reading("co2", 520, start)
        db.insert_reading("co2", 560, start + 5 * 3600)
        db.set_active_scene("Sleeping", start)
        self.activate("Day")
        co2 = db.get_setting("last_sleep_summary")["co2"]
        self.assertEqual(co2["delta"], 40)
        self.assertFalse(co2["rose_significantly"])

    def test_summary_survives_empty_window(self):
        db.set_active_scene("Sleeping", time.time() - 3600)
        self.assertEqual(self.activate("Day").status_code, 200)
        summary = db.get_setting("last_sleep_summary")
        self.assertIsNone(summary["temp"]["avg"])
        self.assertIsNone(summary["co2"]["avg"])
        self.assertIsNone(summary["co2"]["delta"])
        self.assertFalse(summary["co2"]["rose_significantly"])
        self.assertEqual(summary["motion"]["count"], 0)

    def test_no_summary_for_away_to_day(self):
        self.activate("Away")
        data = self.activate("Day").get_json()
        self.assertFalse(data["summary_generated"])
        self.assertIsNone(db.get_setting("last_sleep_summary"))

    def test_no_summary_before_first_transition(self):
        resp = self.client.get("/api/scenes/last-summary")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()["summary"])


if __name__ == "__main__":
    unittest.main()
