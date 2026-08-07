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
        for key in ("state", "detail", "target_lux", "measured_lux", "brightness", "lit"):
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
    """The on/off switch is a master gate, and it is a STORED setting: it says
    "may this lamp light at all", which is a different question from whether the
    bulb is lit right now. The user owns the first, the loop owns the second.

    So a disarmed zone is never lit, and — the part that makes the switch a
    useful readout — an armed zone whose room is already bright enough stays
    armed with the bulb switched off. These tests drive the real loop body (one
    pass, no thread) against mock bulbs.
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
            db.set_device_switch(zone_id, True)
            lighting.zones[zone_id].set_state(on=True, brightness=120)
        lighting._brightness = 120
        lighting._last_change_at = 0.0
        lighting._last_reading_ts = None
        lighting._step_scale, lighting._last_sign = 1.0, 0
        lighting.status = dict(lighting.status, state="holding")
        # a dark room, so the loop wants MORE light — the direction that would
        # expose an unwanted on=True on a disarmed zone
        db.insert_reading("lux", 0)

    def tearDown(self):
        for zone_id in self.zone_ids:
            db.set_device_mode(zone_id, "manual")
            db.set_device_switch(zone_id, True)

    def _disarm(self, zone_id):
        """What the dashboard switch does: store the gate AND put the bulb out."""
        db.set_device_switch(zone_id, False)
        lighting.zones[zone_id].set_state(on=False)

    def test_a_disarmed_zone_is_never_turned_back_on(self):
        for zone_id in self.zone_ids:
            self._disarm(zone_id)
        lighting._one_tick()
        for zone_id in self.zone_ids:
            self.assertFalse(lighting.zones[zone_id].state()["on"],
                             "auto mode turned a disarmed zone back on")

    def test_an_armed_zone_still_gets_its_brightness_driven(self):
        before = lighting.zones[self.zone_ids[0]].state()["brightness"]
        lighting._one_tick()
        after = lighting.zones[self.zone_ids[0]].state()["brightness"]
        self.assertNotEqual(after, before, "brightness should have moved in a dark room")
        self.assertTrue(lighting.zones[self.zone_ids[0]].state()["on"])

    def test_one_zone_disarmed_does_not_stop_the_other(self):
        off_id, on_id = self.zone_ids[0], self.zone_ids[1]
        self._disarm(off_id)
        before = lighting.zones[on_id].state()["brightness"]
        lighting._one_tick()
        self.assertFalse(lighting.zones[off_id].state()["on"])
        self.assertNotEqual(lighting.zones[on_id].state()["brightness"], before)

    def test_the_loop_never_reads_the_gate_back_off_the_bulb(self):
        """The regression this whole design turns on. The loop switches an armed
        bulb off when the room is bright; if it then inferred the gate from the
        bulb's `on`, it would read its own switch-off as the user disarming the
        zone and the lamp would never come back."""
        zone = lighting.zones[self.zone_ids[0]]
        zone.set_state(on=False)          # as if the loop had put it out
        lighting._one_tick()              # room is dark, so it wants light again
        self.assertTrue(zone.state()["on"], "an armed zone stayed dark in a dark room")

    def test_all_zones_disarmed_holds_the_integrator_instead_of_winding_up(self):
        """Otherwise the loop integrates against a room it cannot affect, and
        arming a zone again blasts whatever it wound up to."""
        for zone_id in self.zone_ids:
            self._disarm(zone_id)
        for _ in range(20):
            lighting._one_tick()
        self.assertEqual(lighting._brightness, 120, "integrator wound up while blind")
        self.assertEqual(lighting.status["state"], lighting_control.ZONES_OFF)
        self.assertIn("will not turn it on", lighting.status["detail"])

    def test_arming_again_resumes_from_the_held_value(self):
        for zone_id in self.zone_ids:
            self._disarm(zone_id)
        for _ in range(10):
            lighting._one_tick()
        db.set_device_switch(self.zone_ids[0], True)
        lighting._one_tick()
        # resumes near where it was held, not at the ceiling
        self.assertLessEqual(lighting._brightness, 120 + config.LIGHTING_MAX_STEP)
        self.assertNotEqual(lighting.status["state"], lighting_control.ZONES_OFF)

    def _tick_with_fresh_lux(self, lux, ticks):
        """Drive several corrections. The settle guard normally allows one
        correction per sensor sample, so feed a new sample each tick.

        Timestamps continue past the last sample this helper produced rather
        than restarting at now(): a second call has to extend the timeline, or
        its readings sort older than the first call's and are both rejected by
        the settle guard and passed over by latest_readings()."""
        original = config.LIGHTING_SETTLE_S
        config.LIGHTING_SETTLE_S = 0
        try:
            base = max(time.time(), (lighting._last_reading_ts or 0) + 1)
            for i in range(ticks):
                with db.connect() as conn:
                    conn.execute("INSERT INTO readings (metric, ts, value) VALUES ('lux', ?, ?)",
                                 (base + i, lux))
                lighting._one_tick()
        finally:
            config.LIGHTING_SETTLE_S = original

    def test_a_bright_room_switches_an_armed_zone_off_rather_than_dimming_it(self):
        """Brightness 0 means "emit no light", and the Shelly floors a 0 % push
        to 1 % — so bottoming out leaves a visible glow. Switch the bulb off
        instead."""
        zone = lighting.zones[self.zone_ids[0]]
        self._tick_with_fresh_lux(500, 30)          # far brighter than the target
        self.assertEqual(lighting._brightness, 0, "should have bottomed out")
        self.assertFalse(zone.state()["on"],
                         "left the lamp glowing at the 1 % floor in a bright room")
        self.assertEqual(lighting.status["state"], lighting_control.TOO_BRIGHT)
        self.assertFalse(lighting.status["lit"])

    def test_a_zone_switched_off_for_brightness_stays_armed_and_comes_back(self):
        """The point of the whole arrangement: the switch keeps reading ON
        through a bright afternoon, so you can see which lamps will come up when
        the room dims — and they do come up."""
        zone_id = self.zone_ids[0]
        self._tick_with_fresh_lux(500, 30)
        self.assertFalse(lighting.zones[zone_id].state()["on"])
        self.assertTrue(db.device_switch_on(db.get_device(zone_id)),
                        "the loop disarmed a zone it merely switched off")

        self._tick_with_fresh_lux(0, 3)             # night falls
        self.assertTrue(lighting.zones[zone_id].state()["on"],
                        "an armed zone did not come back on when the room went dark")
        self.assertGreater(lighting.zones[zone_id].state()["brightness"], 0)

    def test_target_zero_touches_nothing_at_all(self):
        """"Auto lighting off" must mean hands off — not "switch every armed
        zone off", which would be indistinguishable from the loop deciding the
        room is bright enough."""
        db.set_lighting({"target_lux": 0})
        lighting.zones[self.zone_ids[0]].set_state(on=True, brightness=90)
        lighting._one_tick()
        state = lighting.zones[self.zone_ids[0]].state()
        self.assertEqual(state["brightness"], 90)
        self.assertTrue(state["on"])


class SeedBrightnessTestCase(unittest.TestCase):
    """What the integrator starts from after a restart. A bulb keeps its
    brightness attribute while switched off, so "the zone reports 140" does not
    mean "the room wants 140" — it may be the level the loop last decided
    against."""

    @classmethod
    def setUpClass(cls):
        db.init_db()
        for device in db.list_devices():
            if device["type"] == "bulb_zone":
                lighting.zones[device["id"]] = make_bulb(device["ip"])
        cls.zone_ids = sorted(lighting.zones)

    def setUp(self):
        for zone_id in self.zone_ids:
            db.set_device_switch(zone_id, True)
        lighting._brightness = -1        # so the seed is unmistakably the source

    def tearDown(self):
        for zone_id in self.zone_ids:
            db.set_device_switch(zone_id, True)

    def test_a_lit_zone_seeds_its_own_brightness(self):
        for zone_id in self.zone_ids:
            lighting.zones[zone_id].set_state(on=True, brightness=96)
        lighting._seed_brightness()
        self.assertEqual(lighting._brightness, 96)

    def test_armed_but_dark_zones_seed_zero_not_their_last_lit_level(self):
        """Otherwise every restart in a bright room flashes the lamps on at the
        level the loop had already walked away from, then walks it back down."""
        for zone_id in self.zone_ids:
            lighting.zones[zone_id].set_state(on=True, brightness=140)
            lighting.zones[zone_id].set_state(on=False)   # as the loop does
        lighting._seed_brightness()
        self.assertEqual(lighting._brightness, 0)

    def test_a_disarmed_zone_is_not_consulted(self):
        """A disarmed zone says nothing about what the controller wanted."""
        for zone_id in self.zone_ids:
            db.set_device_switch(zone_id, False)
            lighting.zones[zone_id].set_state(on=True, brightness=200)
        lighting._seed_brightness()
        self.assertEqual(lighting._brightness, 0)


class GateEndpointTestCase(unittest.TestCase):
    """POST /api/devices/:id/state carries the master gate, and in 'auto' the
    switch the dashboard draws is that gate — not whether the bulb is lit."""

    @classmethod
    def setUpClass(cls):
        db.init_db()
        for device in db.list_devices():
            if device["type"] == "bulb_zone":
                lighting.zones[device["id"]] = make_bulb(device["ip"])
        app = Flask(__name__)
        app.register_blueprint(api)
        cls.client = app.test_client()
        cls.zone_id = sorted(lighting.zones)[0]

    def setUp(self):
        db.set_device_mode(self.zone_id, "auto")
        db.set_device_switch(self.zone_id, True)
        lighting._brightness = 120

    def tearDown(self):
        db.set_device_mode(self.zone_id, "manual")
        db.set_device_switch(self.zone_id, True)

    def _post_on(self, on):
        return self.client.post(f"/api/devices/{self.zone_id}/state",
                                json={"on": on}).get_json()

    def test_the_gate_is_persisted_not_just_pushed_to_the_bulb(self):
        self._post_on(False)
        self.assertFalse(db.device_switch_on(db.get_device(self.zone_id)))
        self._post_on(True)
        self.assertTrue(db.device_switch_on(db.get_device(self.zone_id)))

    def test_arming_a_zone_the_loop_wants_dark_does_not_flash_it_on(self):
        """The controller is holding 0 (bright room), so the lamp must stay off
        — but the switch still has to come back on, or the dashboard snaps it
        straight back to off."""
        lighting._brightness = 0
        body = self._post_on(True)
        self.assertTrue(body["on"], "the switch would snap back off")
        self.assertFalse(body["lit"], "flashed the lamp on in a bright room")
        self.assertFalse(lighting.zones[self.zone_id].state()["on"])

    def test_arming_a_zone_the_loop_wants_lit_lights_it_at_the_held_level(self):
        lighting._brightness = 120
        body = self._post_on(True)
        self.assertTrue(body["on"])
        self.assertTrue(body["lit"])
        self.assertEqual(lighting.zones[self.zone_id].state()["brightness"], 120)

    def test_disarming_puts_the_bulb_out_immediately(self):
        self._post_on(True)
        body = self._post_on(False)
        self.assertFalse(body["on"])
        self.assertFalse(lighting.zones[self.zone_id].state()["on"])

    def test_the_device_list_reports_the_gate_with_the_bulb_state_alongside(self):
        db.set_device_switch(self.zone_id, True)
        lighting.zones[self.zone_id].set_state(on=False)   # as if the loop had
        rows = self.client.get("/api/devices").get_json()
        row = next(r for r in rows if r["id"] == self.zone_id)
        self.assertTrue(row["light"]["on"], "armed zone did not read as on")
        self.assertFalse(row["light"]["lit"])
        self.assertNotEqual(row["auto"]["state"], lighting_control.ZONES_OFF)

    def test_a_disarmed_zone_reports_zones_off_rather_than_the_shared_status(self):
        db.set_device_switch(self.zone_id, False)
        rows = self.client.get("/api/devices").get_json()
        row = next(r for r in rows if r["id"] == self.zone_id)
        self.assertFalse(row["light"]["on"])
        self.assertEqual(row["auto"]["state"], lighting_control.ZONES_OFF)

    def test_manual_zones_report_the_bulb_itself(self):
        """No loop driving them, so the gate and the bulb agree by construction
        — and `on` must keep meaning the bulb, since nothing else sets it."""
        db.set_device_mode(self.zone_id, "manual")
        self.client.post(f"/api/devices/{self.zone_id}/state", json={"on": True})
        rows = self.client.get("/api/devices").get_json()
        row = next(r for r in rows if r["id"] == self.zone_id)
        self.assertTrue(row["light"]["on"])
        self.assertNotIn("auto", row)


if __name__ == "__main__":
    unittest.main()
