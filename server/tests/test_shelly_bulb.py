"""Shelly bulb client: the 0-255 <-> 1-100 % brightness seam and the shape of
the JSON-RPC calls. No network — the transport is stubbed out, so this runs
anywhere the rest of the suite does.

The brightness conversion is the one piece of real arithmetic the Shelly
migration introduced (MANUAL.md 5.4): the hub speaks 0-255 everywhere, the
bulb speaks 1-100, and the translation is supposed to live in exactly one
place.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOCK_HARDWARE"] = "1"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="hub-bulb-test-"), "test.db")

from app.shelly_bulb import (  # noqa: E402
    DEFAULT_COLOR, MockShellyBulb, ShellyBulb, _to_255, _to_pct, make_bulb,
)


class FakeTransport(ShellyBulb):
    """A real ShellyBulb with only _rpc replaced — every layer above it (the
    param building, the conversions, the state unwrapping) is the real code."""

    DEFAULT_STATUS = {"output": True, "brightness": 50,
                      "rgb": [255, 176, 102], "mode": "rgb"}

    def __init__(self, ip="10.0.0.9", status=None):
        super().__init__(ip)
        self.calls = []
        # `is None`, not `or` — an empty status is a case worth testing
        self.status = dict(self.DEFAULT_STATUS) if status is None else status

    def _rpc(self, method, params):
        self.calls.append((method, params))
        return self.status if method == "RGBCCT.GetStatus" else {}


class BrightnessScaleTest(unittest.TestCase):
    def test_hub_scale_maps_onto_the_full_percent_range(self):
        self.assertEqual(_to_pct(255), 100)
        self.assertEqual(_to_pct(128), 50)
        self.assertEqual(_to_pct(180), 71)  # LIGHTING_AUTO_BRIGHTNESS default

    def test_zero_floors_to_one_percent(self):
        # Shelly rejects brightness 0 — "off" is the `on` field's job, so a
        # hub 0 must still produce a legal percent value.
        self.assertEqual(_to_pct(0), 1)

    def test_percent_scale_maps_back(self):
        self.assertEqual(_to_255(100), 255)
        self.assertEqual(_to_255(50), 128)
        self.assertEqual(_to_255(0), 0)

    def test_round_trip_is_stable_within_rounding(self):
        for value in (0, 1, 64, 128, 180, 254, 255):
            self.assertLessEqual(abs(_to_255(_to_pct(value)) - value), 3)


class RpcShapeTest(unittest.TestCase):
    def test_set_state_converts_brightness_and_keeps_component_id(self):
        bulb = FakeTransport()
        bulb.set_state(on=True, brightness=255)
        method, params = bulb.calls[0]
        self.assertEqual(method, "RGBCCT.Set")
        self.assertEqual(params["id"], 0)
        self.assertIs(params["on"], True)
        self.assertEqual(params["brightness"], 100)

    def test_setting_a_color_also_forces_rgb_mode(self):
        # A bulb sitting in "cct" mode ignores rgb outright, so the color
        # would silently do nothing without this.
        bulb = FakeTransport()
        bulb.set_state(color=[10, 20, 30])
        _, params = bulb.calls[0]
        self.assertEqual(params["rgb"], [10, 20, 30])
        self.assertEqual(params["mode"], "rgb")

    def test_partial_update_sends_only_the_named_fields(self):
        bulb = FakeTransport()
        bulb.set_state(on=False)
        _, params = bulb.calls[0]
        self.assertEqual(set(params), {"id", "on"})

    def test_empty_update_makes_no_set_call(self):
        bulb = FakeTransport()
        bulb.set_state()
        self.assertEqual([m for m, _ in bulb.calls], ["RGBCCT.GetStatus"])

    def test_state_unwraps_shelly_fields_into_hub_shape(self):
        bulb = FakeTransport(status={"output": True, "brightness": 50,
                                     "rgb": [1, 2, 3], "ct": 3000, "mode": "rgb"})
        self.assertEqual(bulb.state(), {"on": True, "brightness": 128, "color": [1, 2, 3]})

    def test_state_survives_a_sparse_status(self):
        bulb = FakeTransport(status={})
        self.assertEqual(bulb.state(),
                         {"on": False, "brightness": 0, "color": list(DEFAULT_COLOR)})


class MockBulbTest(unittest.TestCase):
    def test_mock_is_used_under_mock_hardware(self):
        self.assertIsInstance(make_bulb("10.0.0.9"), MockShellyBulb)

    def test_mock_holds_hub_scale_brightness_and_partial_updates(self):
        bulb = MockShellyBulb("10.0.0.9")
        self.assertEqual(bulb.set_state(brightness=200)["brightness"], 200)
        state = bulb.set_state(on=False)
        self.assertEqual(state["brightness"], 200)  # untouched by a partial update
        self.assertIs(state["on"], False)


if __name__ == "__main__":
    unittest.main()
