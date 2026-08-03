"""Auto-lighting tests — the lux -> brightness ramp and its settings endpoint.
Runs against MOCK_HARDWARE=1, no threads, no physical devices. From server/:

    python3 -m unittest discover -s tests
"""

import os
import tempfile
import unittest

# config.py reads these at import time — set them before touching app.*
_TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["MOCK_HARDWARE"] = "1"
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")

from flask import Flask  # noqa: E402

from app import config, db, lighting  # noqa: E402
from app.api import api  # noqa: E402


class LightingRampTestCase(unittest.TestCase):
    """_desired_state is pure — no DB, no bulb — so it tests cheaply."""

    FULL = config.LIGHTING_AUTO_BRIGHTNESS

    def test_pitch_dark_is_full_brightness(self):
        self.assertEqual(lighting._desired_state(0, 50), (True, self.FULL))

    def test_brightness_falls_linearly_with_lux(self):
        # halfway to the cutoff -> half brightness
        self.assertEqual(lighting._desired_state(25, 50), (True, round(self.FULL * 0.5)))
        self.assertEqual(lighting._desired_state(10, 50), (True, round(self.FULL * 0.8)))
        self.assertEqual(lighting._desired_state(40, 50), (True, round(self.FULL * 0.2)))

    def test_off_at_and_above_the_cutoff(self):
        self.assertEqual(lighting._desired_state(50, 50), (False, 0))
        self.assertEqual(lighting._desired_state(500, 50), (False, 0))

    def test_last_stretch_rounds_to_off_not_an_invisible_glow(self):
        # a brightness that rounds to 0 must switch the zone off, not leave it
        # at 1/255 that the Shelly would floor up to a visible 1 %
        on, brightness = lighting._desired_state(49.95, 50)
        self.assertFalse(on)
        self.assertEqual(brightness, 0)

    def test_missing_lux_reading_counts_as_dark(self):
        # sensor loss should leave the room lit, not dark
        self.assertEqual(lighting._desired_state(None, 50), (True, self.FULL))

    def test_zero_cutoff_never_lights_up(self):
        self.assertEqual(lighting._desired_state(0, 0), (False, 0))
        self.assertEqual(lighting._desired_state(None, 0), (False, 0))

    def test_cutoff_scales_the_whole_ramp(self):
        # same lux, a higher cutoff -> brighter, since the room is "darker"
        # relative to the level the user calls fully lit
        _, dim = lighting._desired_state(100, 200)
        _, bright = lighting._desired_state(100, 1000)
        self.assertLess(dim, bright)


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

    def test_defaults_to_the_env_threshold(self):
        data = self.client.get("/api/settings/lighting").get_json()
        self.assertEqual(data["lux_off"], config.LIGHTING_LUX_THRESHOLD)

    def test_put_persists_and_reads_back(self):
        resp = self.client.put("/api/settings/lighting", json={"lux_off": 120})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["lux_off"], 120.0)
        self.assertEqual(db.get_lighting()["lux_off"], 120.0)
        self.assertEqual(
            self.client.get("/api/settings/lighting").get_json()["lux_off"], 120.0)

    def test_validation(self):
        for bad in ({}, {"lux_off": None}, {"lux_off": ""},
                    {"lux_off": "bright"}, {"lux_off": -5}):
            self.assertEqual(
                self.client.put("/api/settings/lighting", json=bad).status_code,
                400, bad)

    def test_zero_is_allowed(self):
        # a deliberate "never light up from lux" setting, not a bad value
        self.assertEqual(
            self.client.put("/api/settings/lighting", json={"lux_off": 0}).status_code,
            200)


if __name__ == "__main__":
    unittest.main()
