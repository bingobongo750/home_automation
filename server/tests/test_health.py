"""Health module pass-1 tests (ingest + raw storage) — same harness as the
other suites: mock hardware, throwaway DB, no threads. From server/:

    python3 -m unittest discover -s tests

Covers: Health Auto Export payload ingestion into the four raw tables,
night assignment (noon-to-noon, keyed to the wake date), both RR item
shapes, idempotent re-ingest, malformed-payload rejection with rollback,
and the latest-night readout endpoint.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

# config.py reads these at import time — set them before touching app.*
_TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["MOCK_HARDWARE"] = "1"
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from flask import Flask  # noqa: E402

from app import db, health  # noqa: E402


def make_client():
    app = Flask(__name__)
    app.register_blueprint(health.bp)
    return app.test_client()


def hae_date(dt: datetime) -> str:
    """A naive local datetime as a Health Auto Export date string
    ("2026-07-08 23:00:00 +0200")."""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


# A fixed reference night: evening of NIGHT-1 into the morning of NIGHT.
BED = datetime(2026, 7, 8, 23, 0)
WAKE = datetime(2026, 7, 9, 7, 0)
NIGHT = "2026-07-09"


def full_payload():
    """One night's worth of every recognized metric."""
    return {"data": {"metrics": [
        {"name": "sleep_analysis", "units": "hr", "data": [
            {"startDate": hae_date(BED), "endDate": hae_date(BED + timedelta(minutes=30)),
             "value": "Core", "qty": 0.5},
            {"startDate": hae_date(BED + timedelta(minutes=30)),
             "endDate": hae_date(BED + timedelta(minutes=90)), "value": "Deep", "qty": 1.0},
            {"startDate": hae_date(BED + timedelta(minutes=90)),
             "endDate": hae_date(BED + timedelta(minutes=95)), "value": "Awake", "qty": 0.08},
            {"startDate": hae_date(BED + timedelta(minutes=95)),
             "endDate": hae_date(WAKE), "value": "REM", "qty": 6.9},
        ]},
        {"name": "heart_rate", "units": "bpm", "data": [
            {"date": hae_date(BED + timedelta(hours=1)), "Min": 48, "Avg": 52, "Max": 61},
            {"date": hae_date(BED + timedelta(hours=4)), "qty": 49},
        ]},
        {"name": "respiratory_rate", "units": "count/min", "data": [
            {"date": hae_date(WAKE), "qty": 14.5},
        ]},
        {"name": "blood_oxygen_saturation", "units": "%", "data": [
            {"date": hae_date(BED + timedelta(hours=2)), "qty": 0.97},
        ]},
        {"name": "apple_sleeping_wrist_temperature", "units": "degC", "data": [
            {"date": hae_date(WAKE), "qty": 34.8},
        ]},
        {"name": "rr_intervals", "units": "ms", "data": [
            {"date": hae_date(BED + timedelta(hours=2)), "intervals": [812.0, 799.5, 820.25]},
            {"date": hae_date(BED + timedelta(hours=5)), "qty": 905.0},
        ]},
    ]}}


class HealthIngestTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        health.init_db()
        cls.client = make_client()

    def setUp(self):
        with db.connect() as conn:
            for table in ("health_rr", "health_sleep_stages",
                          "health_sleep_hr", "health_night_samples"):
                conn.execute(f"DELETE FROM {table}")

    def ingest(self, payload, expect=200):
        resp = self.client.post("/api/health/ingest", json=payload)
        self.assertEqual(resp.status_code, expect, resp.get_json())
        return resp.get_json()

    def count(self, table):
        with db.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    # ---------------------------------------------------------------- happy

    def test_full_payload_stores_all_raw_tables(self):
        out = self.ingest(full_payload())
        self.assertEqual(out["stored"], {
            "rr_intervals": 4, "sleep_stages": 4, "sleep_hr": 2,
            "resp_rate": 1, "spo2": 1, "wrist_temp": 1,
        })
        self.assertEqual(out["duplicates"], 0)
        self.assertEqual(out["ignored"], [])

    def test_night_assignment_noon_to_noon(self):
        # both the pre-midnight and the morning sample land on the wake date
        self.ingest(full_payload())
        with db.connect() as conn:
            nights = {row["night"] for table in
                      ("health_rr", "health_sleep_stages",
                       "health_sleep_hr", "health_night_samples")
                      for row in conn.execute(f"SELECT night FROM {table}")}
        self.assertEqual(nights, {NIGHT})

    def test_rr_run_expands_intervals_with_advancing_timestamps(self):
        start = BED + timedelta(hours=2)
        self.ingest({"data": {"metrics": [
            {"name": "rr_intervals", "data": [
                {"date": hae_date(start), "intervals": [800.0, 750.0]}]},
        ]}})
        with db.connect() as conn:
            rows = conn.execute("SELECT ts, rr_ms FROM health_rr ORDER BY ts").fetchall()
        self.assertEqual([r["rr_ms"] for r in rows], [800.0, 750.0])
        self.assertAlmostEqual(rows[1]["ts"] - rows[0]["ts"], 0.8, places=3)

    def test_reingest_is_idempotent(self):
        first = self.ingest(full_payload())
        again = self.ingest(full_payload())
        self.assertEqual(sum(again["stored"].values()), 0)
        self.assertEqual(again["duplicates"], sum(first["stored"].values()))
        self.assertEqual(self.count("health_rr"), 4)
        self.assertEqual(self.count("health_sleep_stages"), 4)

    def test_unknown_metrics_ignored_not_rejected(self):
        out = self.ingest({"data": {"metrics": [
            {"name": "step_count", "data": [{"date": hae_date(WAKE), "qty": 9000}]},
        ]}})
        self.assertEqual(out["ignored"], ["step_count"])
        self.assertEqual(sum(out["stored"].values()), 0)

    def test_stageless_sleep_rows_skipped(self):
        # "In Bed"/"Asleep" segments (stage-less sources) are valid but unstored
        out = self.ingest({"data": {"metrics": [
            {"name": "sleep_analysis", "data": [
                {"startDate": hae_date(BED), "endDate": hae_date(WAKE), "value": "In Bed"},
            ]},
        ]}})
        self.assertEqual(out["stored"]["sleep_stages"], 0)
        self.assertEqual(self.count("health_sleep_stages"), 0)

    def test_bare_metrics_wrapper_accepted(self):
        out = self.ingest({"metrics": [
            {"name": "respiratory_rate", "data": [{"date": hae_date(WAKE), "qty": 15.0}]},
        ]})
        self.assertEqual(out["stored"]["resp_rate"], 1)

    # ------------------------------------------------------------ rejection

    def test_malformed_payloads_rejected(self):
        for payload in (
            ["not", "an", "object"],
            {"data": {}},
            {"data": {"metrics": "nope"}},
            {"data": {"metrics": [{"name": "heart_rate", "data": "nope"}]}},
            {"data": {"metrics": [{"name": "heart_rate",
                                   "data": [{"date": "yesterday-ish", "qty": 50}]}]}},
            {"data": {"metrics": [{"name": "respiratory_rate",
                                   "data": [{"date": hae_date(WAKE), "qty": "fast"}]}]}},
            {"data": {"metrics": [{"name": "rr_intervals",
                                   "data": [{"date": hae_date(WAKE), "qty": -5}]}]}},
            # aggregated sleep rows (no startDate/endDate/value) get the
            # "disable aggregation" error rather than silent misparse
            {"data": {"metrics": [{"name": "sleep_analysis",
                                   "data": [{"date": hae_date(WAKE), "asleep": 7.5}]}]}},
        ):
            resp = self.client.post("/api/health/ingest", json=payload)
            self.assertEqual(resp.status_code, 400, payload)
            self.assertIn("error", resp.get_json())

    def test_bad_item_rolls_back_whole_payload(self):
        payload = full_payload()
        payload["data"]["metrics"].append(
            {"name": "heart_rate", "data": [{"date": "not a date", "qty": 50}]})
        self.ingest(payload, expect=400)
        for table in ("health_rr", "health_sleep_stages",
                      "health_sleep_hr", "health_night_samples"):
            self.assertEqual(self.count(table), 0, table)

    # -------------------------------------------------------- latest-night

    def test_latest_night_empty(self):
        resp = self.client.get("/api/health/latest-night")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()["night"])

    def test_latest_night_readout(self):
        self.ingest(full_payload())
        out = self.client.get("/api/health/latest-night").get_json()
        self.assertEqual(out["night"], NIGHT)
        self.assertEqual(out["rr"]["count"], 4)
        self.assertEqual(out["sleep_hr"]["count"], 2)
        self.assertEqual(out["sleep_hr"]["min_bpm"], 49)
        self.assertEqual([s["stage"] for s in out["sleep_stages"]],
                         ["core", "deep", "awake", "rem"])
        self.assertEqual(out["samples"]["resp_rate"][0]["value"], 14.5)
        self.assertEqual(out["samples"]["spo2"][0]["value"], 0.97)
        self.assertEqual(out["samples"]["wrist_temp"][0]["value"], 34.8)

    def test_latest_night_picks_newest(self):
        self.ingest(full_payload())
        self.ingest({"data": {"metrics": [
            {"name": "respiratory_rate",
             "data": [{"date": hae_date(WAKE + timedelta(days=1)), "qty": 16.0}]},
        ]}})
        out = self.client.get("/api/health/latest-night").get_json()
        self.assertEqual(out["night"], "2026-07-10")
        self.assertEqual(out["rr"]["count"], 0)  # RR belongs to the older night


# A night with enough RR beats in a deep segment to produce a real RMSSD:
# 60 beats alternating 800/820 ms -> successive diffs +/-20 -> RMSSD 20.
def rr_night(bed: datetime, wake: datetime, beats: int = 60) -> dict:
    intervals = [800.0 if i % 2 == 0 else 820.0 for i in range(beats)]
    rr_start = bed + timedelta(hours=1)
    return {"data": {"metrics": [
        {"name": "sleep_analysis", "data": [
            {"startDate": hae_date(bed), "endDate": hae_date(wake), "value": "Deep"}]},
        {"name": "rr_intervals", "data": [
            {"date": hae_date(rr_start), "intervals": intervals}]},
        {"name": "heart_rate", "data": [
            {"date": hae_date(bed + timedelta(hours=2)), "Avg": 50},
            {"date": hae_date(bed + timedelta(hours=4)), "Avg": 55}]},
        {"name": "respiratory_rate", "data": [{"date": hae_date(wake), "qty": 14.5}]},
        {"name": "blood_oxygen_saturation", "data": [{"date": hae_date(wake), "qty": 0.97}]},
        {"name": "apple_sleeping_wrist_temperature", "data": [{"date": hae_date(wake), "qty": 34.8}]},
    ]}}


# A realistic hypnogram (minutes from bed): core/deep/rem with one awake break.
# TST 230, WASO 10, 1 awakening, REM 80, deep 60. Plus HR (for recovery index)
# and a respiratory sample.
_HYPNOGRAM = [("Core", 0, 30), ("Deep", 30, 90), ("REM", 90, 120),
              ("Awake", 120, 130), ("Core", 130, 190), ("REM", 190, 240)]


def hypnogram_night(bed: datetime) -> dict:
    stage_data = [{"startDate": hae_date(bed + timedelta(minutes=s)),
                   "endDate": hae_date(bed + timedelta(minutes=e)), "value": v}
                  for v, s, e in _HYPNOGRAM]
    return {"data": {"metrics": [
        {"name": "sleep_analysis", "data": stage_data},
        {"name": "heart_rate", "data": [
            {"date": hae_date(bed + timedelta(minutes=40)), "Avg": 48},
            {"date": hae_date(bed + timedelta(minutes=200)), "Avg": 55}]},
        {"name": "respiratory_rate", "data": [
            {"date": hae_date(bed + timedelta(minutes=240)), "qty": 14.0}]},
    ]}}


class HealthDerivedTestCase(unittest.TestCase):
    """Passes 2-5: RR cleaning -> per-night metrics -> baselines -> recovery
    and sleep scores, driven end-to-end through the ingest -> recompute path."""

    ALL_TABLES = ("health_rr", "health_sleep_stages", "health_sleep_hr",
                  "health_night_samples", "health_night_metrics",
                  "health_baselines", "health_scores", "health_sleep_scores",
                  "health_subjective")

    @classmethod
    def setUpClass(cls):
        db.init_db()
        health.init_db()
        cls.client = make_client()

    def setUp(self):
        with db.connect() as conn:
            for table in self.ALL_TABLES:
                conn.execute(f"DELETE FROM {table}")
            # the personal-need setting persists in `settings`; reset per test
            conn.execute("DELETE FROM settings WHERE key = 'health_sleep_need_min'")

    def ingest(self, payload):
        resp = self.client.post("/api/health/ingest", json=payload)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def night(self, query=""):
        resp = self.client.get(f"/api/health/night{query}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_ingest_computes_night_metrics(self):
        night = health.night_of((BED + timedelta(hours=1)).timestamp())
        out = self.ingest(rr_night(BED, WAKE))
        self.assertEqual(out["recomputed"], [night])
        m = self.night()["metrics"]
        self.assertAlmostEqual(m["rmssd"], 20.0, places=4)
        self.assertAlmostEqual(m["ln_rmssd"], __import__("math").log(20.0), places=4)
        self.assertEqual(m["rr_windows"], 1)
        self.assertEqual(m["rr_artifact_pct"], 0.0)
        self.assertAlmostEqual(m["rhr"], 50.25, places=2)   # p5 of [50, 55]
        self.assertAlmostEqual(m["resp_rate"], 14.5)
        self.assertAlmostEqual(m["spo2"], 97.0)             # 0.97 fraction -> percent
        self.assertAlmostEqual(m["wrist_temp"], 34.8)

    def test_score_present_and_bounded(self):
        self.ingest(rr_night(BED, WAKE))
        data = self.night()
        score = data["score"]
        self.assertIsNotNone(score["recovery"])
        self.assertGreaterEqual(score["recovery"], 0.0)
        self.assertLessEqual(score["recovery"], 100.0)
        self.assertTrue(score["provisional"])               # one night of data
        self.assertEqual(score["flags"], [])                # nothing off
        self.assertIn("hrv", score["contributions"])
        # single night -> zero variability -> average maps to 50
        self.assertAlmostEqual(score["recovery"], 50.0, places=4)

    def test_baseline_readout_present(self):
        self.ingest(rr_night(BED, WAKE))
        baselines = self.night()["baselines"]
        self.assertIn("ln_rmssd", baselines)
        self.assertEqual(baselines["ln_rmssd"]["n"], 1)
        self.assertTrue(baselines["ln_rmssd"]["provisional"])

    def test_duplicate_ingest_recomputes_nothing(self):
        self.ingest(rr_night(BED, WAKE))
        again = self.ingest(rr_night(BED, WAKE))
        self.assertEqual(again["recomputed"], [])           # no new raw rows

    def test_baseline_warms_up_over_nights(self):
        warmup = health.config.HEALTH_BASELINE_WARMUP_NIGHTS
        for offset in range(warmup):
            bed = BED - timedelta(days=offset)
            self.ingest(rr_night(bed, bed + timedelta(hours=8)))
        latest = self.night()
        self.assertEqual(latest["baselines"]["ln_rmssd"]["n"], warmup)
        self.assertFalse(latest["baselines"]["ln_rmssd"]["provisional"])
        self.assertFalse(latest["score"]["provisional"])

    def test_recompute_all_endpoint(self):
        self.ingest(rr_night(BED, WAKE))
        resp = self.client.post("/api/health/recompute")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["recomputed_nights"], 1)

    def test_night_endpoint_null_before_data(self):
        self.assertIsNone(self.night()["night"])

    # ------------------------------------------------ pass 5: sleep score

    def test_sleep_metrics_and_score(self):
        self.ingest(hypnogram_night(BED))
        data = self.night()
        m = data["metrics"]
        self.assertAlmostEqual(m["tst_min"], 230.0)
        self.assertAlmostEqual(m["waso_min"], 10.0)
        self.assertEqual(m["awakenings"], 1)
        self.assertAlmostEqual(m["rem_min"], 80.0)
        self.assertAlmostEqual(m["deep_min"], 60.0)

        sleep = data["sleep"]
        self.assertIsNotNone(sleep["sleep_score"])
        self.assertGreaterEqual(sleep["sleep_score"], 0.0)
        self.assertLessEqual(sleep["sleep_score"], 100.0)
        self.assertEqual(sleep["consistency_src"], "sd_fallback")
        # duration sub-score: 35 pts lost per hour short of the 480min need —
        # 230min is >4h short, so it floors at 0
        self.assertAlmostEqual(sleep["subscores"]["duration"]["value"], 0.0, places=3)
        self.assertEqual(sleep["subscores"]["waso"]["value"], 100.0)
        self.assertEqual(sleep["subscores"]["awakenings"]["value"], 100.0)
        self.assertIsNotNone(sleep["recovery_index"])
        self.assertTrue(sleep["provisional"])

    def test_no_sleep_stages_means_no_sleep_score(self):
        self.ingest(rr_night(BED, WAKE))          # only a Deep block -> has sleep
        self.ingest({"data": {"metrics": [        # a night with only an awake row
            {"name": "sleep_analysis", "data": [
                {"startDate": hae_date(BED + timedelta(days=2)),
                 "endDate": hae_date(BED + timedelta(days=2, minutes=20)), "value": "Awake"}]},
        ]}})
        # latest night (awake-only) has no scored sleep
        self.assertIsNone(self.night()["sleep"])

    def test_sleep_need_setting_recomputes_duration(self):
        self.ingest(hypnogram_night(BED))
        resp = self.client.put("/api/health/settings", json={"sleep_need_min": 230})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["sleep_need_min"], 230.0)
        # need now equals TST -> duration sub-score maxes out
        sleep = self.night()["sleep"]
        self.assertAlmostEqual(sleep["subscores"]["duration"]["value"], 100.0)

    def test_sleep_need_setting_validated(self):
        self.assertEqual(self.client.put("/api/health/settings",
                                         json={"sleep_need_min": 5000}).status_code, 400)

    def test_consistency_populates_over_several_nights(self):
        for offset in range(3):
            bed = BED - timedelta(days=offset)
            self.ingest(hypnogram_night(bed))
        sleep = self.night()["sleep"]
        self.assertIsNotNone(sleep["subscores"]["consistency"]["value"])

    def test_single_night_has_no_sri(self):
        self.ingest(hypnogram_night(BED))
        sleep = self.night()["sleep"]
        self.assertIsNone(sleep["sri"])
        self.assertEqual(sleep["consistency_src"], "sd_fallback")

    # ------------------------------------------- pass 6: deep-dive metrics

    def test_deep_dive_metrics(self):
        for offset in range(3):                    # 3 consecutive nights, TST 230 each
            bed = BED - timedelta(days=offset)
            self.ingest(hypnogram_night(bed))
        sleep = self.night()["sleep"]
        # restorative % = (deep 60 + rem 80) / tst 230
        self.assertAlmostEqual(sleep["restorative_pct"], 140 / 230 * 100, places=2)
        # debt = 3 nights * (480 need - 230) = 750; target = 480 + capped payback
        self.assertAlmostEqual(sleep["sleep_debt_min"], 750.0, places=1)
        self.assertAlmostEqual(sleep["target_sleep_min"], 570.0, places=1)  # 480 + min(90, .5*750)
        # >= 2 nights -> SRI drives Consistency
        self.assertIsNotNone(sleep["sri"])
        self.assertEqual(sleep["consistency_src"], "sri")
        self.assertAlmostEqual(sleep["subscores"]["consistency"]["value"], sleep["sri"])

    # ----------------------------------------------- pass 7: UI/digest data

    def test_night_includes_stages_for_hypnogram(self):
        self.ingest(hypnogram_night(BED))
        stages = self.night()["stages"]
        self.assertEqual([s["stage"] for s in stages],
                         ["core", "deep", "rem", "awake", "core", "rem"])

    def test_history_endpoint(self):
        for offset in range(3):
            bed = BED - timedelta(days=offset)
            self.ingest(hypnogram_night(bed))
        resp = self.client.get("/api/health/history?range=30d")
        self.assertEqual(resp.status_code, 200)
        out = resp.get_json()
        self.assertEqual(out["range"], "30d")
        self.assertEqual(len(out["nights"]), 3)
        latest = out["nights"][-1]
        self.assertIsNotNone(latest["sleep_score"])
        self.assertIn("rec_provisional", latest)

    def test_morning_snapshot(self):
        self.ingest(hypnogram_night(BED))
        snap = health.morning_snapshot((WAKE).timestamp())
        self.assertEqual(snap["night"], NIGHT)
        self.assertIn("sleep", snap)
        self.assertIsNotNone(snap["sleep"]["score"])
        self.assertIsNotNone(snap["sleep"]["target_sleep_min"])

    def test_morning_snapshot_none_without_data(self):
        self.assertIsNone(health.morning_snapshot(WAKE.timestamp()))

    # -------------------------------------- pass 8: subjective + correlation

    def rate(self, night, rating):
        resp = self.client.post("/api/health/subjective", json={"night": night, "rating": rating})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def test_subjective_rating_stored_and_surfaced(self):
        self.ingest(hypnogram_night(BED))
        self.rate(NIGHT, 4)
        self.assertEqual(self.night()["subjective"]["rating"], 4)
        self.rate(NIGHT, 2)  # upsert
        self.assertEqual(self.night()["subjective"]["rating"], 2)

    def test_subjective_rating_validated(self):
        self.assertEqual(self.client.post("/api/health/subjective",
                                          json={"rating": 9}).status_code, 400)
        self.assertEqual(self.client.post("/api/health/subjective",
                                          json={"rating": "good"}).status_code, 400)

    def test_correlation(self):
        # ingest 5 nights and rate them in line with their sleep scores
        nights = []
        for offset in range(5):
            bed = BED - timedelta(days=offset)
            self.ingest(hypnogram_night(bed))
            nights.append(health.night_of((bed + timedelta(minutes=1)).timestamp()))
        for i, night in enumerate(nights):
            self.rate(night, (i % 5) + 1)
        out = self.client.get("/api/health/correlation").get_json()
        self.assertEqual(out["ratings"], 5)
        self.assertIsInstance(out["recovery"]["n"], int)
        # r is either a float or null (null if a series had no variance)
        self.assertTrue(out["sleep"]["r"] is None or -1.0 <= out["sleep"]["r"] <= 1.0)

    def test_correlation_null_before_enough_ratings(self):
        self.ingest(hypnogram_night(BED))
        self.rate(NIGHT, 3)
        out = self.client.get("/api/health/correlation").get_json()
        self.assertEqual(out["ratings"], 1)
        self.assertIsNone(out["recovery"]["r"])


if __name__ == "__main__":
    unittest.main()
