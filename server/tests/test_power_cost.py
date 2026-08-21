"""Plug energy and what it cost: the trapezoidal integral behind `kwh_24h` /
`kwh_7d`, the tariff those are priced at, and the settings endpoint that owns
it. Runs against MOCK_HARDWARE=1. From server/:

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

from app import db  # noqa: E402
from app.api import api  # noqa: E402


class PowerCostTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        app = Flask(__name__)
        app.register_blueprint(api)
        cls.client = app.test_client()
        cls.device_id = next(d["id"] for d in db.list_devices() if d["type"] == "wifi_plug")

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM power_readings WHERE device_id = ?", (self.device_id,))
            conn.execute("DELETE FROM settings WHERE key = 'electricity'")

    def _samples(self, watts, count, step=10.0, end=None, start_at=None):
        """`count` samples `step` seconds apart, ending now unless told otherwise."""
        end = time.time() if end is None else end
        first = end - (count - 1) * step if start_at is None else start_at
        with db.connect() as conn:
            for i in range(count):
                conn.execute(
                    "INSERT INTO power_readings (ts, device_id, watts, relay_on) "
                    "VALUES (?, ?, ?, 1)",
                    (first + i * step, self.device_id, watts),
                )

    # ---- the integral itself ----

    def test_constant_draw_integrates_to_watts_times_hours(self):
        # 100 W held for exactly one hour (361 samples, 10 s apart) = 100 Wh.
        self._samples(100.0, 361, step=10.0)
        stats = db.power_stats(self.device_id)
        self.assertAlmostEqual(stats["kwh_24h"], 0.1, places=3)
        self.assertAlmostEqual(stats["avg_24h_w"], 100.0, places=1)

    def test_gap_is_skipped_not_bridged(self):
        """A stretch where the plug was unreachable must not be billed.

        Two one-hour blocks of 100 W with five hours of nothing between them is
        0.2 kWh of measured energy. Average draw is 100 W across a 7 h span, so
        avg x duration would call it 0.7 kWh — it would bill the hole at the
        rate either side of it, which is the whole reason this is a trapezoid
        over the samples that exist.
        """
        now = time.time()
        self._samples(100.0, 361, step=10.0, start_at=now - 7 * 3600)  # 1 h
        self._samples(100.0, 361, step=10.0, start_at=now - 3600)      # 1 h, 5 h later
        stats = db.power_stats(self.device_id)
        self.assertAlmostEqual(stats["avg_24h_w"], 100.0, places=1)
        self.assertAlmostEqual(stats["kwh_24h"], 0.2, places=3)

    def test_no_samples_reports_none_not_zero(self):
        stats = db.power_stats(self.device_id)
        self.assertIsNone(stats["kwh_24h"])
        self.assertIsNone(stats["kwh_7d"])
        self.assertIsNone(stats["cost_24h"])
        self.assertIsNone(stats["cost_7d"])

    def test_single_sample_is_not_energy(self):
        self._samples(100.0, 1)
        self.assertIsNone(db.power_stats(self.device_id)["kwh_24h"])

    def test_7d_window_covers_more_than_24h(self):
        now = time.time()
        self._samples(100.0, 361, step=10.0, start_at=now - 3 * 86400)  # 1 h, 3 days ago
        self._samples(100.0, 361, step=10.0, start_at=now - 3600)       # 1 h, today
        stats = db.power_stats(self.device_id)
        self.assertAlmostEqual(stats["kwh_24h"], 0.1, places=3)
        self.assertAlmostEqual(stats["kwh_7d"], 0.2, places=3)

    # ---- pricing ----

    def test_cost_is_energy_times_tariff(self):
        db.set_electricity({"price_per_kwh": 0.30, "currency": "CHF"})
        self._samples(100.0, 361, step=10.0)          # 0.1 kWh
        stats = db.power_stats(self.device_id)
        self.assertAlmostEqual(stats["cost_24h"], 0.03, places=4)
        self.assertEqual(stats["currency"], "CHF")
        self.assertEqual(stats["price_per_kwh"], 0.30)

    def test_zero_tariff_means_unknown_not_free(self):
        db.set_electricity({"price_per_kwh": 0, "currency": "CHF"})
        self._samples(100.0, 361, step=10.0)
        stats = db.power_stats(self.device_id)
        self.assertIsNotNone(stats["kwh_24h"])
        self.assertIsNone(stats["cost_24h"])

    # ---- the settings endpoint ----

    def test_settings_roundtrip(self):
        res = self.client.put("/api/settings/electricity",
                              json={"price_per_kwh": 0.2745, "currency": "EUR"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"price_per_kwh": 0.2745, "currency": "EUR"})
        self.assertEqual(self.client.get("/api/settings/electricity").get_json(),
                         {"price_per_kwh": 0.2745, "currency": "EUR"})

    def test_settings_defaults_to_env_seed(self):
        from app import config
        got = self.client.get("/api/settings/electricity").get_json()
        self.assertEqual(got["price_per_kwh"], config.ELECTRICITY_PRICE_PER_KWH)
        self.assertEqual(got["currency"], config.ELECTRICITY_CURRENCY)

    def test_blank_price_is_accepted_as_zero(self):
        res = self.client.put("/api/settings/electricity",
                              json={"price_per_kwh": "", "currency": "CHF"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["price_per_kwh"], 0)

    def test_bad_price_is_rejected(self):
        for body in ({"price_per_kwh": "free"}, {"price_per_kwh": -1}):
            res = self.client.put("/api/settings/electricity", json=body)
            self.assertEqual(res.status_code, 400, body)

    def test_stats_endpoint_carries_cost(self):
        db.set_electricity({"price_per_kwh": 0.30, "currency": "CHF"})
        self._samples(100.0, 361, step=10.0)
        got = self.client.get(f"/api/devices/{self.device_id}/power/stats").get_json()
        for key in ("kwh_24h", "kwh_7d", "cost_24h", "cost_7d", "currency", "price_per_kwh"):
            self.assertIn(key, got)
        self.assertAlmostEqual(got["cost_24h"], 0.03, places=4)


if __name__ == "__main__":
    unittest.main()
