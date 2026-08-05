"""Auto-lighting: the settings endpoint and the job's timing/staleness logic.

The control law itself is pure and lives in app/lighting_control.py — its
convergence behaviour is covered far more thoroughly in
tests/test_lighting_control.py. What is left for here is everything that needs
the DB or a clock: the user-owned setpoint, and the "never correct twice on the
same measurement" guard that keeps the loop from overshooting.

Runs against MOCK_HARDWARE=1, no threads, no physical devices. From server/:

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

from app import config, db, lighting, lighting_control  # noqa: E402
from app.shelly_bulb import make_bulb  # noqa: E402
from app.api import api  # noqa: E402


class LightingSettingsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        app = Flask(__name__)
        app.register_blueprint(api)
        cls.client = app.test_client()

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM settings")

    def test_defaults_to_the_env_target(self):
        data = self.client.get("/api/settings/lighting").get_json()
        self.assertEqual(data["target_lux"], config.LIGHTING_TARGET_LUX)

    def test_put_persists_and_reads_back(self):
        resp = self.client.put("/api/settings/lighting", json={"target_lux": 12})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["target_lux"], 12.0)
        self.assertEqual(db.get_lighting()["target_lux"], 12.0)
        self.assertEqual(
            self.client.get("/api/settings/lighting").get_json()["target_lux"], 12.0)

    def test_validation(self):
        for bad in ({}, {"target_lux": None}, {"target_lux": ""},
                    {"target_lux": "bright"}, {"target_lux": -5}):
            self.assertEqual(
                self.client.put("/api/settings/lighting", json=bad).status_code,
                400, bad)

    def test_zero_is_allowed(self):
        # a deliberate "never light up from lux" setting, not a bad value
        self.assertEqual(
            self.client.put("/api/settings/lighting", json={"target_lux": 0}).status_code,
            200)

    def test_a_stored_lux_off_is_ignored_not_reinterpreted(self):
        """The old open-loop setting is not convertible into a setpoint — one was
        the top of a fade, the other is a target — so an install carrying it
        must fall back to the env default rather than silently adopt 50 lx as
        the level to hold the room at."""
        db.set_setting("lighting", {"lux_off": 50})
        self.assertEqual(db.get_lighting()["target_lux"], config.LIGHTING_TARGET_LUX)
        self.assertNotIn("lux_off", db.get_lighting())


class StalenessGuardTestCase(unittest.TestCase):
    """The job must not correct on a reading that cannot describe its own last
    change. Both halves are plain arithmetic on timestamps, so they are checked
    here directly rather than by running the thread."""

    def _stale(self, *, reading_ts, last_change_at, last_reading_ts):
        # mirrors the condition in lighting._auto_loop
        if reading_ts is None:
            return False
        if reading_ts < last_change_at + config.LIGHTING_SETTLE_S:
            return True
        return last_reading_ts is not None and reading_ts <= last_reading_ts

    def test_reading_from_before_the_change_is_stale(self):
        now = time.time()
        self.assertTrue(self._stale(reading_ts=now, last_change_at=now,
                                    last_reading_ts=None))

    def test_reading_after_the_settle_window_is_usable(self):
        now = time.time()
        self.assertFalse(self._stale(reading_ts=now + config.LIGHTING_SETTLE_S + 1,
                                     last_change_at=now, last_reading_ts=None))

    def test_the_same_sample_is_never_acted_on_twice(self):
        now = time.time()
        self.assertTrue(self._stale(reading_ts=now, last_change_at=0,
                                    last_reading_ts=now))
        self.assertFalse(self._stale(reading_ts=now + 1, last_change_at=0,
                                     last_reading_ts=now))

    def test_stale_holds_brightness_instead_of_guessing(self):
        result = lighting_control.correct(
            measured_lux=999, target_lux=5, brightness=77, max_brightness=180,
            deadband_lux=1.0, gain=6.0, max_step=16, reading_stale=True)
        self.assertEqual(result.state, lighting_control.STALE)
        self.assertEqual(result.brightness, 77)


class StatusShapeTestCase(unittest.TestCase):
    """GET /api/devices carries the controller's state on auto zones, which is
    how the card can say 'the room is already too bright' out loud."""

    @classmethod
    def setUpClass(cls):
        db.init_db()
        app = Flask(__name__)
        app.register_blueprint(api)
        cls.client = app.test_client()

    def test_status_has_the_keys_the_dashboard_reads(self):
        for key in ("state", "detail", "target_lux", "measured_lux", "brightness"):
            self.assertIn(key, lighting.status)

    def test_auto_zone_rows_carry_the_controller_state(self):
        zone = next(d for d in db.list_devices() if d["type"] == "bulb_zone")
        db.set_device_mode(zone["id"], "auto")
        try:
            rows = self.client.get("/api/devices").get_json()
            row = next(r for r in rows if r["id"] == zone["id"])
            self.assertIn("auto", row)
            self.assertIn("detail", row["auto"])
        finally:
            db.set_device_mode(zone["id"], "manual")

    def test_manual_zone_rows_do_not(self):
        zone = next(d for d in db.list_devices() if d["type"] == "bulb_zone")
        db.set_device_mode(zone["id"], "manual")
        rows = self.client.get("/api/devices").get_json()
        row = next(r for r in rows if r["id"] == zone["id"])
        self.assertNotIn("auto", row)

    def test_every_state_has_a_human_sentence(self):
        for state in (lighting_control.HOLDING, lighting_control.CONVERGING,
                      lighting_control.TOO_BRIGHT, lighting_control.AT_MAX,
                      lighting_control.OFF_BY_SETTING, lighting_control.NO_READING,
                      lighting_control.STALE):
            text = lighting_control.describe(state, target_lux=5.0, measured_lux=3.0)
            self.assertTrue(text and text.endswith("."), state)


class MasterSwitchTestCase(unittest.TestCase):
    """The on/off switch is a master gate: auto sets brightness, the user
    decides which lamps may light at all.

    Before this, the job pushed `on=True` every tick, so switching a zone off
    while it was in auto mode undid itself seconds later. These tests drive the
    real loop body (one pass, no thread) against mock bulbs.
    """

    @classmethod
    def setUpClass(cls):
        db.init_db()
        for device in db.list_devices():
            if device["type"] == "bulb_zone":
                lighting.zones[device["id"]] = make_bulb(device["ip"])
        cls.zone_ids = sorted(lighting.zones)

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM readings")
        for zone_id in self.zone_ids:
            db.set_device_mode(zone_id, "auto")
            lighting.zones[zone_id].set_state(on=True, brightness=120)
        lighting._brightness = 120
        lighting._last_change_at = 0.0
        lighting._last_reading_ts = None
        lighting._step_scale, lighting._last_sign = 1.0, 0
        lighting.status = dict(lighting.status, state="holding")
        # a dark room, so the loop wants MORE light — the direction that would
        # expose an unwanted on=True
        db.insert_reading("lux", 0)

    def tearDown(self):
        for zone_id in self.zone_ids:
            db.set_device_mode(zone_id, "manual")

    def test_a_switched_off_zone_is_never_turned_back_on(self):
        for zone_id in self.zone_ids:
            lighting.zones[zone_id].set_state(on=False)
        lighting._one_tick()
        for zone_id in self.zone_ids:
            self.assertFalse(lighting.zones[zone_id].state()["on"],
                             "auto mode turned a switched-off zone back on")

    def test_a_switched_on_zone_still_gets_its_brightness_driven(self):
        before = lighting.zones[self.zone_ids[0]].state()["brightness"]
        lighting._one_tick()
        after = lighting.zones[self.zone_ids[0]].state()["brightness"]
        self.assertNotEqual(after, before, "brightness should have moved in a dark room")
        self.assertTrue(lighting.zones[self.zone_ids[0]].state()["on"])

    def test_one_zone_off_does_not_stop_the_other(self):
        off_id, on_id = self.zone_ids[0], self.zone_ids[1]
        lighting.zones[off_id].set_state(on=False)
        before = lighting.zones[on_id].state()["brightness"]
        lighting._one_tick()
        self.assertFalse(lighting.zones[off_id].state()["on"])
        self.assertNotEqual(lighting.zones[on_id].state()["brightness"], before)

    def test_all_zones_off_holds_the_integrator_instead_of_winding_up(self):
        """Otherwise the loop integrates against a room it cannot affect, and
        flipping a switch back on blasts whatever it wound up to."""
        for zone_id in self.zone_ids:
            lighting.zones[zone_id].set_state(on=False)
        for _ in range(20):
            lighting._one_tick()
        self.assertEqual(lighting._brightness, 120, "integrator wound up while blind")
        self.assertEqual(lighting.status["state"], lighting_control.ZONES_OFF)
        self.assertIn("will not turn it on", lighting.status["detail"])

    def test_switching_back_on_resumes_from_the_held_value(self):
        for zone_id in self.zone_ids:
            lighting.zones[zone_id].set_state(on=False)
        for _ in range(10):
            lighting._one_tick()
        lighting.zones[self.zone_ids[0]].set_state(on=True, brightness=120)
        lighting._one_tick()
        # resumes near where it was held, not at the ceiling
        self.assertLessEqual(lighting._brightness, 120 + config.LIGHTING_MAX_STEP)
        self.assertNotEqual(lighting.status["state"], lighting_control.ZONES_OFF)

    def _tick_with_fresh_lux(self, lux, ticks):
        """Drive several corrections. The settle guard normally allows one
        correction per sensor sample, so feed a new sample each tick."""
        original = config.LIGHTING_SETTLE_S
        config.LIGHTING_SETTLE_S = 0
        try:
            base = time.time()
            for i in range(ticks):
                with db.connect() as conn:
                    conn.execute("INSERT INTO readings (metric, ts, value) VALUES ('lux', ?, ?)",
                                 (base + i, lux))
                lighting._one_tick()
        finally:
            config.LIGHTING_SETTLE_S = original

    def test_a_switched_on_zone_is_never_switched_off_by_the_loop(self):
        """The gate cuts both ways. Driving brightness to 0 in a bright room
        must NOT push `on=False` — that would take the switch away from the user
        just as surely as forcing `on=True` did. The zone stays on, bottomed out.

        This is the case the `live` filter alone does not cover, so it is what
        actually pins `set_state(brightness=...)` with no `on`.
        """
        zone = lighting.zones[self.zone_ids[0]]
        self._tick_with_fresh_lux(500, 30)          # far brighter than the target
        self.assertEqual(lighting._brightness, 0, "should have bottomed out")
        state = zone.state()
        self.assertEqual(state["brightness"], 0)
        self.assertTrue(state["on"],
                        "loop switched off a zone the user had switched on")
        self.assertEqual(lighting.status["state"], lighting_control.TOO_BRIGHT)

    def test_target_zero_touches_nothing_at_all(self):
        """"Auto lighting off" must mean hands off — not "dim every zone to the
        Shelly's 1 % floor", which is what pushing brightness 0 would do now
        that the loop no longer sends `on`."""
        db.set_lighting({"target_lux": 0})
        lighting.zones[self.zone_ids[0]].set_state(on=True, brightness=90)
        lighting._one_tick()
        state = lighting.zones[self.zone_ids[0]].state()
        self.assertEqual(state["brightness"], 90)
        self.assertTrue(state["on"])


if __name__ == "__main__":
    unittest.main()
