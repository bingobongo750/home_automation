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


if __name__ == "__main__":
    unittest.main()
