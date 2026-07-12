"""Pure health-math tests (app/health_compute.py) — no DB, no Flask.
Covers passes 2-4: RR cleaning + RMSSD, resting HR, rolling baselines, and the
z-score recovery model.

    python3 -m unittest discover -s tests
"""

import math
import unittest

from app import health_compute as hc


class RRCleaningTestCase(unittest.TestCase):
    def test_rmssd_known_value(self):
        # diffs [10, -20] -> squares [100, 400] -> mean 250 -> sqrt 15.811
        self.assertAlmostEqual(hc.rmssd([800, 810, 790]), math.sqrt(250), places=3)

    def test_rmssd_needs_two_beats(self):
        self.assertIsNone(hc.rmssd([800]))

    def test_out_of_bounds_beat_flagged_and_interpolated(self):
        corrected, artifact = hc.clean_rr_window([800.0, 810.0, 3000.0, 805.0])
        self.assertAlmostEqual(artifact, 0.25)          # 1 of 4 beats
        self.assertAlmostEqual(corrected[2], 807.5)     # linear between 810 and 805

    def test_relative_threshold_flags_ectopic_beat(self):
        # 1100 is >20% off its local median (~807) — an ectopic/dropped beat
        corrected, artifact = hc.clean_rr_window([800.0, 805.0, 810.0, 1100.0, 808.0, 806.0])
        self.assertAlmostEqual(artifact, 1 / 6)
        self.assertAlmostEqual(corrected[3], 809.0)     # between 810 and 808

    def test_clean_window_leaves_good_data_untouched(self):
        vals = [800.0, 810.0, 795.0, 805.0, 802.0]
        corrected, artifact = hc.clean_rr_window(vals)
        self.assertEqual(artifact, 0.0)
        self.assertEqual(corrected, vals)


class NightHRVTestCase(unittest.TestCase):
    def _beats(self, start, values):
        """[(ts, rr_ms)] where each beat's ts advances by the prior interval."""
        samples, ts = [], start
        for v in values:
            samples.append((ts, v))
            ts += v / 1000.0
        return samples

    def test_hrv_over_deep_window(self):
        vals = [800.0, 820.0] * 20            # 40 beats, alternating -> RMSSD 20
        samples = self._beats(1000.0, vals)
        stages = [("deep", 999.0, 1100.0)]    # segment covers all the beats
        out = hc.compute_hrv(samples, stages)
        self.assertAlmostEqual(out["rmssd"], 20.0, places=6)
        self.assertAlmostEqual(out["ln_rmssd"], math.log(20.0), places=6)
        self.assertEqual(out["windows"], 1)
        self.assertEqual(out["artifact_pct"], 0.0)

    def test_hrv_none_when_too_few_beats(self):
        samples = self._beats(1000.0, [800.0] * 5)  # under RR_MIN_BEATS
        out = hc.compute_hrv(samples, [("deep", 999.0, 1100.0)])
        self.assertIsNone(out["rmssd"])
        self.assertEqual(out["windows"], 0)

    def test_hrv_restricts_to_stable_stages(self):
        # 40 good beats in deep, plus junk beats during an awake segment
        deep = self._beats(1000.0, [800.0, 820.0] * 20)
        awake = self._beats(2000.0, [500.0, 1500.0] * 20)
        stages = [("deep", 999.0, 1100.0), ("awake", 1999.0, 2100.0)]
        out = hc.compute_hrv(deep + awake, stages)
        self.assertAlmostEqual(out["rmssd"], 20.0, places=6)  # awake window ignored


class RestimgHRAndBaselineTestCase(unittest.TestCase):
    def test_resting_hr_is_low_percentile(self):
        # sorted [47,48,49,50,52,60], p5 -> 47 + (48-47)*0.25
        self.assertAlmostEqual(hc.resting_hr([50, 48, 52, 60, 47, 49]), 47.25)

    def test_normalize_spo2_fraction_to_percent(self):
        self.assertAlmostEqual(hc.normalize_spo2(0.97), 97.0)
        self.assertAlmostEqual(hc.normalize_spo2(96.0), 96.0)  # already percent

    def test_baseline_stats_and_swc(self):
        b = hc.baseline([10.0, 12.0, 14.0], [14.0], warmup=14)
        self.assertAlmostEqual(b["mean"], 12.0)
        self.assertAlmostEqual(b["sd"], 2.0)          # sample SD of 10,12,14
        self.assertAlmostEqual(b["swc_low"], 11.0)    # mean - 0.5*SD
        self.assertAlmostEqual(b["swc_high"], 13.0)
        self.assertAlmostEqual(b["trend_7"], 14.0)
        self.assertEqual(b["n"], 3)
        self.assertTrue(b["provisional"])             # 3 < 14

    def test_baseline_not_provisional_when_warm(self):
        b = hc.baseline([50.0] * 20, [50.0] * 7, warmup=14)
        self.assertFalse(b["provisional"])
        self.assertEqual(b["sd"], 0.0)

    def test_baseline_none_without_data(self):
        self.assertIsNone(hc.baseline([], [], warmup=14))


CFG = {
    "w_hrv": 0.60, "w_rhr": 0.25, "w_rr": 0.15,
    "temp_dev_c": 0.5, "spo2_dip_pct": 93.0, "rr_spike_br": 1.0,
    "penalty_temp": 10.0, "penalty_spo2": 8.0, "penalty_rr": 6.0,
}


def base(mean, sd, provisional=False):
    return {"mean": mean, "sd": sd, "provisional": provisional}


class RecoveryScoreTestCase(unittest.TestCase):
    def _baselines(self, provisional=False):
        return {
            "ln_rmssd": base(math.log(60), 0.2, provisional),
            "rhr": base(52.0, 3.0, provisional),
            "resp_rate": base(14.0, 0.8, provisional),
            "wrist_temp": base(34.8, 0.15, provisional),
            "spo2": base(97.0, 1.0, provisional),
        }

    def test_average_night_scores_fifty(self):
        metrics = {"ln_rmssd": math.log(60), "rhr": 52.0, "resp_rate": 14.0,
                   "wrist_temp": 34.8, "spo2": 97.0}
        out = hc.recovery_score(metrics, self._baselines(), CFG)
        self.assertAlmostEqual(out["score"], 50.0, places=6)
        self.assertEqual(out["flags"], [])

    def test_better_night_scores_above_fifty(self):
        # higher HRV, lower RHR/RR than baseline -> better recovery
        metrics = {"ln_rmssd": math.log(75), "rhr": 48.0, "resp_rate": 13.0,
                   "wrist_temp": 34.8, "spo2": 97.0}
        out = hc.recovery_score(metrics, self._baselines(), CFG)
        self.assertGreater(out["score"], 60.0)
        self.assertGreater(out["contributions"]["hrv"]["z"], 0)

    def test_flag_penalties_subtract(self):
        metrics = {"ln_rmssd": math.log(60), "rhr": 52.0, "resp_rate": 14.0,
                   "wrist_temp": 35.6, "spo2": 90.0}   # temp +0.8, SpO2 dip
        out = hc.recovery_score(metrics, self._baselines(), CFG)
        self.assertIn("temp_deviation", out["flags"])
        self.assertIn("spo2_dip", out["flags"])
        self.assertAlmostEqual(out["penalty"], 18.0)
        self.assertAlmostEqual(out["score"], out["base_score"] - 18.0)

    def test_rr_spike_flag(self):
        metrics = {"ln_rmssd": math.log(60), "rhr": 52.0, "resp_rate": 15.5,
                   "wrist_temp": 34.8, "spo2": 97.0}   # +1.5 br/min over baseline
        out = hc.recovery_score(metrics, self._baselines(), CFG)
        self.assertIn("rr_spike", out["flags"])

    def test_weights_renormalize_over_available_metrics(self):
        metrics = {"ln_rmssd": math.log(75), "rhr": None, "resp_rate": None,
                   "wrist_temp": None, "spo2": None}
        baselines = self._baselines()
        out = hc.recovery_score(metrics, baselines, CFG)
        # only HRV present -> z_total equals the raw HRV z (weight renormalised)
        expected_z = (math.log(75) - baselines["ln_rmssd"]["mean"]) / baselines["ln_rmssd"]["sd"]
        self.assertAlmostEqual(out["z_total"], expected_z, places=6)
        self.assertIsNone(out["contributions"]["rhr"]["z"])

    def test_provisional_follows_hrv_baseline(self):
        metrics = {"ln_rmssd": math.log(60), "rhr": 52.0, "resp_rate": 14.0}
        self.assertTrue(hc.recovery_score(metrics, self._baselines(provisional=True), CFG)["provisional"])
        self.assertFalse(hc.recovery_score(metrics, self._baselines(provisional=False), CFG)["provisional"])

    def test_no_metrics_yields_null_score(self):
        out = hc.recovery_score({}, {}, CFG)
        self.assertIsNone(out["score"])
        self.assertTrue(out["provisional"])


class SleepStageTestCase(unittest.TestCase):
    def _seg(self, stage, s, e):  # minutes -> (stage, ts, ts)
        return (stage, s * 60.0, e * 60.0)

    def test_stage_metrics(self):
        stages = [
            self._seg("core", 0, 30), self._seg("deep", 30, 90),
            self._seg("rem", 90, 120), self._seg("awake", 120, 130),
            self._seg("core", 130, 190), self._seg("rem", 190, 240),
        ]
        m = hc.sleep_stage_metrics(stages)
        self.assertAlmostEqual(m["tst_min"], 230.0)     # 90 core + 60 deep + 80 rem
        self.assertAlmostEqual(m["rem_min"], 80.0)
        self.assertAlmostEqual(m["deep_min"], 60.0)
        self.assertAlmostEqual(m["waso_min"], 10.0)     # the one awake segment
        self.assertEqual(m["awakenings"], 1)
        self.assertAlmostEqual(m["tib_min"], 240.0)
        self.assertAlmostEqual(m["onset_ts"], 0.0)
        self.assertAlmostEqual(m["wake_ts"], 240.0 * 60)
        self.assertAlmostEqual(m["rem_frac"], 80 / 230)

    def test_no_sleep_returns_none(self):
        self.assertIsNone(hc.sleep_stage_metrics([self._seg("awake", 0, 30)]))
        self.assertIsNone(hc.sleep_stage_metrics([]))


class SleepSubscoreTestCase(unittest.TestCase):
    def test_duration_ramp_and_oversleep(self):
        # shortfall costs 35 pts/h (default): 7h against an 8h need = 65
        self.assertAlmostEqual(hc.duration_subscore(420, 480, 60, 240), 65.0)
        self.assertAlmostEqual(hc.duration_subscore(240, 480, 60, 240), 0.0)   # 4h short -> floor
        self.assertAlmostEqual(hc.duration_subscore(480, 480, 60, 240), 100.0)
        self.assertAlmostEqual(hc.duration_subscore(520, 480, 60, 240), 100.0)  # within tolerance
        self.assertLess(hc.duration_subscore(800, 480, 60, 240), 100.0)         # big oversleep
        # penalty steepness is tunable
        self.assertAlmostEqual(hc.duration_subscore(420, 480, 60, 240, 20.0), 80.0)

    def test_inverse_ramps(self):
        self.assertEqual(hc.waso_subscore(10, 20, 90), 100.0)
        self.assertEqual(hc.waso_subscore(90, 20, 90), 0.0)
        self.assertAlmostEqual(hc.waso_subscore(55, 20, 90), 50.0)
        self.assertEqual(hc.awakenings_subscore(1, 1, 8), 100.0)
        self.assertEqual(hc.awakenings_subscore(8, 1, 8), 0.0)

    def test_stage_deficit_only_penalises_below_typical(self):
        self.assertEqual(hc.stage_deficit_subscore(0.25, 0.22), 100.0)   # excess not over-rewarded
        self.assertEqual(hc.stage_deficit_subscore(0.22, 0.22), 100.0)
        self.assertEqual(hc.stage_deficit_subscore(0.11, 0.22), 0.0)     # at typical/2
        self.assertAlmostEqual(hc.stage_deficit_subscore(0.165, 0.22), 50.0)

    def test_timing_consistency(self):
        # identical times over nights -> SD 0 -> 100
        self.assertAlmostEqual(hc.timing_consistency_subscore([660, 660, 660], [420, 420, 420], 120), 100.0)
        # one night can't yield an SD
        self.assertIsNone(hc.timing_consistency_subscore([660], [420], 120))

    def test_sleep_score_weighted_and_renormalised(self):
        subs = {"duration": 50.0, "waso": 100.0, "consistency": None,
                "rem": 100.0, "awakenings": 100.0, "deep": 100.0}
        weights = {"duration": 35, "waso": 20, "consistency": 17,
                   "rem": 12, "awakenings": 8, "deep": 8}
        out = hc.sleep_score(subs, weights)
        # consistency absent -> renormalise over the other 83 weight
        expected = (35 * 50 + 20 * 100 + 12 * 100 + 8 * 100 + 8 * 100) / 83
        self.assertAlmostEqual(out["score"], expected)
        self.assertIsNone(out["subscores"]["consistency"]["value"])

    def test_recovery_index_rewards_early_low_and_long_after(self):
        # HR low near the start, ~6h of sleep after it
        samples = [(0, 60), (600, 50), (7 * 3600, 62)]
        out = hc.recovery_index(samples, 0, 7 * 3600)
        self.assertTrue(out["low_in_first_half"])
        self.assertGreater(out["index"], 90.0)
        self.assertIsNone(hc.recovery_index([(0, 60)], 0, 3600))  # too few points


class DeepDiveTestCase(unittest.TestCase):
    def test_restorative_pct(self):
        self.assertAlmostEqual(hc.restorative_pct(60, 80, 230), (140 / 230) * 100)
        self.assertIsNone(hc.restorative_pct(60, 80, 0))

    def test_sleep_debt_accumulates_deficit_and_discounts_surplus(self):
        # two short nights (60 short each) + one long (60 surplus, discounted)
        debt = hc.sleep_debt([420, 420, 540], need=480, surplus_discount=0.5)
        self.assertAlmostEqual(debt, 60 + 60 - 30)   # 90
        self.assertEqual(hc.sleep_debt([600, 600], 480, 0.5), 0.0)  # floored at 0

    def test_target_sleep_caps_payback(self):
        self.assertAlmostEqual(hc.target_sleep(480, 100, 0.5, 90), 530.0)   # 0.5*100=50
        self.assertAlmostEqual(hc.target_sleep(480, 400, 0.5, 90), 570.0)   # capped at 90

    def test_pearson(self):
        # perfectly correlated
        self.assertAlmostEqual(hc.pearson([(1, 2), (2, 4), (3, 6), (4, 8)]), 1.0)
        # perfectly anti-correlated
        self.assertAlmostEqual(hc.pearson([(1, 8), (2, 6), (3, 4), (4, 2)]), -1.0)
        # too few pairs / no variance -> None
        self.assertIsNone(hc.pearson([(1, 2), (2, 4)]))
        self.assertIsNone(hc.pearson([(3, 5), (3, 9), (3, 1)]))

    def test_sri_perfect_and_irregular(self):
        # identical sleep window each day -> SRI 100
        day = 86400
        spans = [(i * day + 0, i * day + 8 * 3600) for i in range(8)]
        sri = hc.sleep_regularity_index(spans, 0, 8 * day, 300)
        self.assertGreater(sri, 99.0)   # ~100; the final day has no +24h partner
        # anti-phase (sleep window shifts 12h every other day) -> low SRI
        alt = []
        for i in range(8):
            off = 0 if i % 2 == 0 else 12 * 3600
            alt.append((i * day + off, i * day + off + 8 * 3600))
        self.assertLess(hc.sleep_regularity_index(alt, 0, 8 * day, 300), 60.0)


if __name__ == "__main__":
    unittest.main()
