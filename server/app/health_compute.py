"""Pure health-metric math — no DB, no Flask, no I/O, so it unit-tests cheaply.

Covers passes 2-4 of docs/health-build-plan.md:
- RR artifact cleaning + per-window / per-night RMSSD, lnRMSSD (methodology §1.1)
- nocturnal resting HR and the nightly-vitals reductions
- rolling baselines: 7-day trend, 30-60-day mean/SD, SWC band (methodology §1.2)
- z-score recovery model squashed to 0-100 + flag penalties (§1.3-1.4)

Everything here operates on plain lists/dicts the caller pulled out of SQLite
and returns plain dicts the caller writes back, so app/health.py owns all the
persistence and this stays trivially testable. The scoring keeps every
intermediate (per-metric z, weights, flags) so the UI can explain a score and
a later weight change can recompute it from stored history — never re-ingest.
"""

import math
import statistics

# ------------------------------------------------- small stats helpers

def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def percentile(xs, p):
    """Linear-interpolated p-th percentile (0-100), or None if empty."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def phi(z):
    """Standard normal CDF — squashes z_total so an average night maps to ~50."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ------------------------------------------- RR cleaning / RMSSD (§1.1)

RR_MIN_MS = 300.0         # physiological bounds — HR 30-200 bpm
RR_MAX_MS = 2000.0
RR_REL_THRESHOLD = 0.20   # flag beats >20% off the local median (ectopic/dropped)
RR_LOCAL_SPAN = 5         # neighbours used for the local median
RR_WINDOW_SEC = 300       # split stable sleep into ~5-min windows
RR_GAP_SEC = 60           # a gap longer than this breaks a window (sensor dropout)
RR_MIN_BEATS = 20         # too few beats to trust a window's RMSSD
RR_MAX_ARTIFACT = 0.05    # discard a window if >5% of its beats were artifacts
STABLE_STAGES = ("deep", "core")


def clean_rr_window(values):
    """Artifact-correct one window of RR intervals (ms). Returns
    (corrected_values, artifact_fraction). Pipeline (methodology §1.1):
    physiological bounds -> relative local-median threshold -> interpolate
    flagged beats (not delete) so the successive-difference adjacency that
    RMSSD depends on is preserved."""
    n = len(values)
    if n == 0:
        return [], 0.0
    in_bounds = [RR_MIN_MS <= v <= RR_MAX_MS for v in values]
    flagged = [not b for b in in_bounds]
    half = RR_LOCAL_SPAN // 2
    for i in range(n):
        if flagged[i]:
            continue
        neigh = [values[j] for j in range(max(0, i - half), min(n, i + half + 1))
                 if in_bounds[j] and j != i]
        if len(neigh) >= 2:
            m = median(neigh)
            if m and abs(values[i] - m) / m > RR_REL_THRESHOLD:
                flagged[i] = True
    corrected = list(values)
    for i in range(n):
        if not flagged[i]:
            continue
        left = i - 1
        while left >= 0 and flagged[left]:
            left -= 1
        right = i + 1
        while right < n and flagged[right]:
            right += 1
        if left >= 0 and right < n:
            corrected[i] = (corrected[left]
                            + (corrected[right] - corrected[left]) * (i - left) / (right - left))
        elif left >= 0:
            corrected[i] = corrected[left]
        elif right < n:
            corrected[i] = corrected[right]
        # else: every beat flagged — leave as-is, the window gets discarded
    return corrected, sum(flagged) / n


def rmssd(values):
    """Root-mean-square of successive RR differences, or None if <2 beats."""
    if len(values) < 2:
        return None
    diffs = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))


def stable_windows(rr_samples, stages):
    """Group timestamped RR samples [(ts, rr_ms), ...] into windows over stable
    (deep/core) sleep. Falls back to the whole night when no deep/core segments
    exist. A window breaks on a >RR_GAP_SEC gap or after RR_WINDOW_SEC."""
    samples = sorted(rr_samples)
    segs = [(s, e) for (st, s, e) in stages if st in STABLE_STAGES]
    if segs:
        samples = [(ts, rr) for (ts, rr) in samples
                   if any(s <= ts < e for (s, e) in segs)]
    windows, cur = [], []
    win_start = prev_ts = None
    for ts, rr in samples:
        if not cur:
            cur, win_start, prev_ts = [rr], ts, ts
            continue
        if ts - prev_ts > RR_GAP_SEC or ts - win_start > RR_WINDOW_SEC:
            windows.append(cur)
            cur, win_start = [rr], ts
        else:
            cur.append(rr)
        prev_ts = ts
    if cur:
        windows.append(cur)
    return windows


def compute_hrv(rr_samples, stages):
    """Per-night RMSSD + lnRMSSD from beat-to-beat RR. Cleans each stable
    window, discards windows over the artifact ceiling, and takes the median
    window RMSSD as the night's value (a robust central choice, per §1.1).
    Returns {rmssd, ln_rmssd, artifact_pct, windows}; rmssd/ln_rmssd are None
    if no window survived cleaning."""
    windows = stable_windows(rr_samples, stages)
    win_rmssds = []
    beats = flagged = 0
    for w in windows:
        if len(w) < RR_MIN_BEATS:
            continue
        corrected, artifact = clean_rr_window(w)
        beats += len(w)
        flagged += artifact * len(w)
        if artifact > RR_MAX_ARTIFACT:
            continue
        r = rmssd(corrected)
        if r is not None:
            win_rmssds.append(r)
    artifact_pct = round(flagged / beats * 100.0, 2) if beats else None
    if not win_rmssds:
        return {"rmssd": None, "ln_rmssd": None,
                "artifact_pct": artifact_pct, "windows": 0}
    night = median(win_rmssds)
    return {"rmssd": night, "ln_rmssd": math.log(night),
            "artifact_pct": artifact_pct, "windows": len(win_rmssds)}


RHR_PERCENTILE = 5  # nocturnal low, robust against a couple of artifact lows


def resting_hr(hr_values):
    """Nocturnal resting HR: a low percentile of the sleep-HR series (the
    lowest / late-night point the recovery world uses), robust to spikes."""
    return percentile(hr_values, RHR_PERCENTILE)


def normalize_spo2(value):
    """Apple exports SpO2 as a 0-1 fraction; store/score it as a percent."""
    if value is None:
        return None
    return value * 100.0 if value <= 1.5 else value


# --------------------------------------------------- baselines (§1.2)

def baseline(long_vals, short_vals, warmup):
    """Rolling per-metric baseline: long-window mean/SD (the normal-range
    anchor for scoring), short-window trend, SWC band (mean +/- 0.5*SD) and CV.
    Marked provisional until `warmup` nights of data exist (§3 warm-up)."""
    long_vals = [v for v in long_vals if v is not None]
    if not long_vals:
        return None
    m = statistics.fmean(long_vals)
    sd = statistics.stdev(long_vals) if len(long_vals) >= 2 else 0.0
    short = [v for v in short_vals if v is not None]
    swc = 0.5 * sd
    return {
        "mean": m,
        "sd": sd,
        "trend_7": statistics.fmean(short) if short else None,
        "swc_low": m - swc,
        "swc_high": m + swc,
        "cv": (sd / m * 100.0) if m else None,
        "n": len(long_vals),
        "provisional": len(long_vals) < warmup,
    }


# --------------------------------------------- recovery score (§1.3-1.4)

def _z(value, base, invert=False):
    """Signed z-score of `value` against a baseline dict, or None if either is
    missing. Zero variability (sd==0, warm-up) yields a neutral 0."""
    if value is None or base is None:
        return None
    if not base["sd"]:
        return 0.0
    z = (value - base["mean"]) / base["sd"]
    return -z if invert else z


def recovery_score(metrics, baselines, cfg):
    """z-score recovery model -> 0-100 (methodology §1.4). Per-metric z vs the
    user's own baseline, sign-adjusted (HRV higher = better, RHR/RR lower =
    better), weighted, then squashed with the normal CDF so an average night
    maps to ~50. Flag penalties (temp deviation / SpO2 dip / RR spike) are
    subtracted on top — "something is off" states the linear model shouldn't
    smooth over. Weights renormalise over whatever metrics are present.

    `metrics`: {ln_rmssd, rhr, resp_rate, wrist_temp, spo2} (any may be None).
    `baselines`: {metric_name: baseline-dict or None}.
    `cfg`: weights + penalty thresholds (see config.HEALTH_*).
    Returns the score and every intermediate for transparent UI + recompute.
    """
    terms = [
        ("hrv", _z(metrics.get("ln_rmssd"), baselines.get("ln_rmssd")), cfg["w_hrv"]),
        ("rhr", _z(metrics.get("rhr"), baselines.get("rhr"), invert=True), cfg["w_rhr"]),
        ("rr", _z(metrics.get("resp_rate"), baselines.get("resp_rate"), invert=True), cfg["w_rr"]),
    ]
    contributions = {}
    wsum = ztotal = 0.0
    used = 0
    for name, z, w in terms:
        if z is None:
            contributions[name] = {"z": None, "weight": w, "contribution": None}
            continue
        wsum += w
        ztotal += w * z
        used += 1
        contributions[name] = {"z": z, "weight": w, "contribution": w * z}

    if not used:
        return {"score": None, "base_score": None, "z_total": None,
                "contributions": contributions, "flags": [], "penalty": 0.0,
                "provisional": True}

    ztotal /= wsum  # renormalise over the metrics actually available
    base_score = 100.0 * phi(ztotal)

    flags, penalty = [], 0.0
    temp_base = baselines.get("wrist_temp")
    if (metrics.get("wrist_temp") is not None and temp_base
            and abs(metrics["wrist_temp"] - temp_base["mean"]) > cfg["temp_dev_c"]):
        flags.append("temp_deviation")
        penalty += cfg["penalty_temp"]
    if metrics.get("spo2") is not None and metrics["spo2"] < cfg["spo2_dip_pct"]:
        flags.append("spo2_dip")
        penalty += cfg["penalty_spo2"]
    rr_base = baselines.get("resp_rate")
    if (metrics.get("resp_rate") is not None and rr_base
            and metrics["resp_rate"] > rr_base["mean"] + cfg["rr_spike_br"]):
        flags.append("rr_spike")
        penalty += cfg["penalty_rr"]

    hrv_base = baselines.get("ln_rmssd")
    return {
        "score": max(0.0, min(100.0, base_score - penalty)),
        "base_score": base_score,
        "z_total": ztotal,
        "contributions": contributions,
        "flags": flags,
        "penalty": penalty,
        # dominant driver's baseline governs confidence (§3 warm-up)
        "provisional": hrv_base is None or hrv_base["provisional"],
    }


# ------------------------------------------------ sleep stages (§2.1)

SLEEP_STAGES = ("rem", "core", "deep")


def sleep_stage_metrics(stages):
    """Reduce a night's stage SEGMENTS [(stage, start_ts, end_ts), ...] to the
    durations the sleep score needs. Sleep onset = first sleep-stage segment;
    final wake = last one; WASO/awakenings count only awake time BETWEEN those
    (in-bed-awake before onset or after final wake isn't disruption). Returns
    None if the night has no scored sleep at all."""
    segs = sorted(stages, key=lambda x: x[1])
    if not segs:
        return None
    sleep = [x for x in segs if x[0] in SLEEP_STAGES]
    if not sleep:
        return None
    onset, wake = sleep[0][1], sleep[-1][2]

    def minutes(pred):
        return sum((e - s) for st, s, e in segs if pred(st)) / 60.0

    rem = minutes(lambda st: st == "rem")
    deep = minutes(lambda st: st == "deep")
    core = minutes(lambda st: st == "core")
    tst = rem + deep + core

    waso = 0.0
    awakenings = 0
    for st, s, e in segs:
        if st != "awake":
            continue
        lo, hi = max(s, onset), min(e, wake)
        if hi > lo:
            waso += (hi - lo) / 60.0
            awakenings += 1

    return {
        "tst_min": tst,
        "tib_min": (segs[-1][2] - segs[0][1]) / 60.0,
        "waso_min": waso,
        "awakenings": awakenings,
        "rem_min": rem,
        "deep_min": deep,
        "core_min": core,
        "onset_ts": onset,
        "wake_ts": wake,
        "rem_frac": rem / tst if tst else None,
        "deep_frac": deep / tst if tst else None,
    }


# ------------------------------------------------ sleep sub-scores (§2.3)

def duration_subscore(tst, need, over_tol, over_zero, short_penalty_per_h=35.0):
    """100 at the personal need (plateau), losing `short_penalty_per_h` points
    per hour of shortfall below it (steeper than the old proportional ramp —
    deliberately: at the default 35/h an 8h need scores 7h at 65, not 87), and
    a mild penalty for large oversleeping past `over_tol` minutes."""
    if tst is None or not need:
        return None
    if tst <= need:
        return max(0.0, 100.0 - short_penalty_per_h * (need - tst) / 60.0)
    over = tst - need
    if over <= over_tol:
        return 100.0
    frac = min((over - over_tol) / max(over_zero - over_tol, 1.0), 1.0)
    return 100.0 - 40.0 * frac  # lose up to 40 pts for extreme oversleep


def _inverse_ramp(value, good, bad):
    """100 at/below `good`, 0 at/above `bad`, linear between."""
    if value is None:
        return None
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return 100.0 * (bad - value) / (bad - good)


def waso_subscore(waso, good, bad):
    return _inverse_ramp(waso, good, bad)


def awakenings_subscore(count, good, bad):
    return _inverse_ramp(count, good, bad)


def stage_deficit_subscore(frac, typical, floor_ratio=0.5):
    """Score a stage proportion against the personal typical: full marks at/above
    typical (don't over-reward excess), linear down to 0 as it falls to
    typical*floor_ratio."""
    if frac is None or not typical:
        return None
    if frac >= typical:
        return 100.0
    low = typical * floor_ratio
    if frac <= low:
        return 0.0
    return 100.0 * (frac - low) / (typical - low)


def timing_consistency_subscore(onset_offsets, wake_offsets, sd_bad):
    """SD fallback for the Consistency component (used until SRI exists, §2.3):
    average SD of sleep-onset and wake times (minutes from each night's anchor
    noon) over the trailing window, inverted so a rock-steady schedule -> 100
    and an SD of `sd_bad` -> 0. None if fewer than two nights."""
    sds = []
    for offsets in (onset_offsets, wake_offsets):
        vals = [o for o in offsets if o is not None]
        if len(vals) >= 2:
            sds.append(statistics.pstdev(vals))
    if not sds:
        return None
    return max(0.0, 100.0 * (1.0 - statistics.fmean(sds) / sd_bad))


def sleep_score(subscores, weights):
    """Weighted average of the 0-100 sub-scores -> 0-100. Weights renormalise
    over whatever sub-scores are present. Keeps each {value, weight} for the UI
    breakdown."""
    total = wsum = 0.0
    detail = {}
    for name, weight in weights.items():
        value = subscores.get(name)
        detail[name] = {"value": value, "weight": weight}
        if value is None:
            continue
        total += weight * value
        wsum += weight
    return {"score": (total / wsum) if wsum else None, "subscores": detail}


def recovery_index(hr_samples, onset_ts, wake_ts):
    """Optional cross-signal from the sleep-HR curve (§2.4): the "good" pattern
    is HR hitting its nightly low early, then hours of sleep after it. Returns
    {min_after_min, low_in_first_half, index (0-100)} or None. `index` rewards
    minutes slept after the HR low (full at ~6h), with a bonus when the low
    lands in the first half of the night."""
    pts = sorted((ts, bpm) for ts, bpm in hr_samples if onset_ts <= ts <= wake_ts)
    if len(pts) < 2 or wake_ts <= onset_ts:
        return None
    low_ts = min(pts, key=lambda p: p[1])[0]
    min_after = (wake_ts - low_ts) / 60.0
    first_half = low_ts <= onset_ts + (wake_ts - onset_ts) / 2.0
    index = min(100.0, min_after / 360.0 * 100.0)
    if first_half:
        index = min(100.0, index + 10.0)
    return {"min_after_min": min_after, "low_in_first_half": first_half,
            "index": index}


# ------------------------------------------ deep-dive metrics (§2.6)

def restorative_pct(deep_min, rem_min, tst_min):
    """(deep + REM) / TST — display only (deep+REM already scored, §2.6). Judge
    against the personal baseline, not a fixed 33%."""
    if not tst_min:
        return None
    return (deep_min + rem_min) / tst_min * 100.0


def sleep_debt(actuals, need, surplus_discount):
    """Rolling (need - actual) over the window, floored at 0. A loose accounting
    model, not physiology: surplus nights repay debt at a discount (diminishing
    returns), and debt older than the window has already rolled off."""
    debt = 0.0
    for tst in actuals:
        if tst is None:
            continue
        deficit = need - tst
        debt += deficit if deficit >= 0 else deficit * surplus_discount
    return max(0.0, debt)


def target_sleep(need, debt, alpha, cap):
    """Forward-looking target for the next night: need + capped debt payback so
    a rough week never demands an 11-hour night (§2.6)."""
    return need + min(cap, alpha * debt)


# ------------------------------------------- tuning / correlation (§3)

def pearson(pairs):
    """Pearson r between two paired series, or None if fewer than 3 complete
    pairs or a series has no variance. For correlating the subjective 1-5
    morning rating against a computed score — the only real way to tune weights
    to your own feel (§3)."""
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def sleep_regularity_index(sleep_intervals, window_start, window_end, epoch_sec):
    """Sleep Regularity Index (§2.6): the probability of being in the same
    sleep/wake state at two times 24h apart, over the window, scored 0-100
    (100 = perfectly regular). `sleep_intervals` is the merged [(start, end)]
    asleep spans; epochs are sampled every `epoch_sec` from window_start up to
    window_end - 24h and compared with their +24h counterpart. Fixed
    noon-to-noon window + 7-day span kept stable so history stays comparable."""
    spans = sorted(sleep_intervals)

    def asleep(ts):
        # spans are few per night; a linear scan is plenty for one user
        return any(s <= ts < e for s, e in spans)

    day = 86400
    t = window_start
    concordant = total = 0
    while t + day <= window_end:
        if asleep(t) == asleep(t + day):
            concordant += 1
        total += 1
        t += epoch_sec
    if not total:
        return None
    sri = 200.0 * (concordant / total) - 100.0  # 50% agreement -> 0
    return max(0.0, sri)
