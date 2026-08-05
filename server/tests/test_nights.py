"""Night history detail: the awakenings re-derivation and the per-night metric
series that back the dialog's "when did I get up" timeline and its expandable
stats. Runs against MOCK_HARDWARE=1. From server/:

    python3 -m unittest discover -s tests
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

# config.py reads these at import time — set them before touching app.*
_TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["MOCK_HARDWARE"] = "1"
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")

from flask import Flask  # noqa: E402

from app import config, db  # noqa: E402
from app.api import api  # noqa: E402


class NightDetailTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        app = Flask(__name__)
        app.register_blueprint(api)
        cls.client = app.test_client()

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM readings")
            conn.execute("DELETE FROM night_summaries")
        # a 9.5 h night ending this morning
        self.end = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        self.start = self.end - timedelta(hours=9, minutes=30)
        self.from_ts = self.start.timestamp()
        self.to_ts = self.end.timestamp()
        with db.connect() as conn:
            ts = self.from_ts
            while ts <= self.to_ts:
                conn.execute("INSERT INTO readings (metric, ts, value) VALUES ('temp', ?, ?)",
                             (ts, 21.0))
                conn.execute("INSERT INTO readings (metric, ts, value) VALUES ('co2', ?, ?)",
                             (ts, 600.0))
                ts += 60
        db.save_night_summary(self.from_ts, self.to_ts,
                              {"from": self.from_ts, "to": self.to_ts,
                               "motion": {"count": 0, "samples": 0, "events": []}})
        self.night = self.end.strftime("%Y-%m-%d")

    def _add_motion(self, offset_s, duration_s, step=30):
        with db.connect() as conn:
            for k in range(0, int(duration_s) + 1, step):
                conn.execute("INSERT INTO readings (metric, ts, value) VALUES ('motion', ?, 1)",
                             (self.from_ts + offset_s + k,))

    # ---- awakenings: WHEN you got up, with how long ----
    def test_awakenings_carry_start_end_and_duration(self):
        self._add_motion(2 * 3600, 0)          # a brief stir
        self._add_motion(5 * 3600, 20 * 60)    # up for 20 minutes
        data = self.client.get(f"/api/nights/{self.night}").get_json()
        ups = data["awakenings"]
        self.assertEqual(len(ups), 2)
        self.assertEqual(round(ups[0]["duration_s"]), 0)
        self.assertEqual(round(ups[1]["duration_s"]), 20 * 60)
        for up in ups:
            self.assertGreaterEqual(up["end"], up["start"])
            self.assertGreaterEqual(up["samples"], 1)
            self.assertTrue(self.from_ts <= up["start"] <= self.to_ts)

    def test_continuous_detection_is_one_awakening_not_many(self):
        """A PIR emits a row per cycle while it sees you; a raw count would
        report one trip to the bathroom as dozens of awakenings."""
        self._add_motion(3 * 3600, 10 * 60, step=10)   # 61 rows
        data = self.client.get(f"/api/nights/{self.night}").get_json()
        self.assertEqual(len(data["awakenings"]), 1)
        self.assertGreater(data["awakenings"][0]["samples"], 10)

    def test_a_gap_longer_than_the_cooldown_starts_a_new_awakening(self):
        self._add_motion(1 * 3600, 60)
        self._add_motion(1 * 3600 + 60 + config.DISTURBANCE_COOLDOWN_S + 60, 60)
        data = self.client.get(f"/api/nights/{self.night}").get_json()
        self.assertEqual(len(data["awakenings"]), 2)

    def test_motion_outside_the_window_is_not_counted(self):
        self._add_motion(-3600, 60)                      # before bedtime
        self._add_motion(10 * 3600 + 3600, 60)           # after getting up
        data = self.client.get(f"/api/nights/{self.night}").get_json()
        self.assertEqual(data["awakenings"], [])

    def test_quiet_night_reports_an_empty_list_not_a_missing_key(self):
        data = self.client.get(f"/api/nights/{self.night}").get_json()
        self.assertEqual(data["awakenings"], [])

    def test_works_for_nights_stored_before_awakenings_existed(self):
        """The stored summary keeps only bare start timestamps; awakenings are
        re-derived from readings, which are never pruned — so an old night gets
        durations too, without a migration."""
        self._add_motion(4 * 3600, 12 * 60)
        db.save_night_summary(self.from_ts, self.to_ts,
                              {"from": self.from_ts, "to": self.to_ts})  # no motion key at all
        data = self.client.get(f"/api/nights/{self.night}").get_json()
        self.assertEqual(len(data["awakenings"]), 1)
        self.assertEqual(round(data["awakenings"][0]["duration_s"]), 12 * 60)

    # ---- per-night metric series ----
    def test_series_covers_exactly_the_stored_window(self):
        resp = self.client.get(f"/api/nights/{self.night}/series?metric=temp")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["from"], self.from_ts)
        self.assertEqual(data["to"], self.to_ts)
        self.assertGreater(len(data["points"]), 10)
        # Bucket labels are FLOORED to the bucket boundary, so the first point
        # can sit up to one bucket before the window start even though every
        # reading averaged into it is inside the window (the SQL restricts to
        # it). /api/sensors/history behaves the same way; clamping the label
        # here would misplace the bucket and make the first interval uneven.
        bucket = (self.to_ts - self.from_ts) / 240
        for point in data["points"]:
            self.assertTrue(self.from_ts - bucket <= point["ts"] <= self.to_ts,
                            f"{point['ts']} more than one bucket outside the night")

    def test_series_bucket_size_does_not_drift_as_the_day_goes_on(self):
        """metric_history buckets against now(), so the same night would come
        back at a different resolution every time it was opened. This one is
        fixed by the window."""
        a = self.client.get(f"/api/nights/{self.night}/series?metric=temp").get_json()
        b = db.metric_window_history("temp", self.from_ts, self.to_ts)
        self.assertEqual(len(a["points"]), len(b))

    def test_series_rejects_metrics_with_no_meaningful_curve(self):
        # motion is discrete events — the timeline is its visualisation, a line
        # through it would invent data
        self.assertEqual(
            self.client.get(f"/api/nights/{self.night}/series?metric=motion").status_code, 400)
        self.assertEqual(
            self.client.get(f"/api/nights/{self.night}/series?metric=nonsense").status_code, 400)

    def test_series_404s_for_an_unknown_night(self):
        self.assertEqual(
            self.client.get("/api/nights/1999-01-01/series?metric=temp").status_code, 404)

    def test_series_available_for_every_metric_the_dialog_offers(self):
        for metric in ("temp", "hum", "co2", "lux"):
            resp = self.client.get(f"/api/nights/{self.night}/series?metric={metric}")
            self.assertEqual(resp.status_code, 200, metric)
            self.assertEqual(resp.get_json()["metric"], metric)


if __name__ == "__main__":
    unittest.main()
