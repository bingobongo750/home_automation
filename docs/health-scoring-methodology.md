# Recovery & Sleep Scoring — Methodology Reference

A design reference for the hub's health module. Synthesised from how Whoop, Oura,
and the sports-science HRV literature actually build these scores. Everything here
is intended to be liftable into incremental Claude Code passes.

---

## 0. The one principle that matters most

**Score against the user's own rolling baseline, not against population norms.**

Whoop, Oura, and every credible HRV-guided-training study do this. A raw HRV of
45ms is meaningless in isolation; a 45ms night when *your* baseline is 70ms is a
strong signal. Population reference tables are only useful as a sanity check, never
as the scoring axis.

Consequence for the architecture: you need a **baseline table** (rolling per-metric
mean + standard deviation) that a nightly batch job maintains, and the scores are
computed as *deviations from that baseline*. This fits the "Mac stays thin" model
perfectly — it's a small nightly aggregation over short time series, no real-time work.

---

## 1. Recovery score

### 1.1 Computing RMSSD correctly from beat-to-beat intervals

You're right that computing your own RMSSD is better than Apple's native HRV value.
Apple's HealthKit HRV is **SDNN** over a short, opportunistic, mostly-daytime window.
RMSSD reflects short-term parasympathetic (vagal) activity and is the metric the
recovery world actually uses.

**RMSSD is defined over successive RR (beat-to-beat) intervals:**

```
RMSSD = sqrt( mean( (RR[i+1] - RR[i])^2 ) )   for i = 1..N-1
```

**Artifact correction is mandatory, not optional.** RMSSD is pathologically sensitive
to bad beats — a single missed or extra beat can inflate it by 300–400%. Pipeline:

1. **Physiological bounds** — drop any RR outside ~300–2000ms (HR 30–200 bpm).
2. **Relative threshold** — flag any RR that differs from the local median (or the
   previous accepted interval) by more than ~20–25%. This catches ectopic beats and
   dropped/doubled beats. The Lipponen–Tarvainen (2019) adaptive dual-threshold
   method is the gold standard here and has open-source implementations; a simple
   percentage-threshold filter is a reasonable v1.
3. **Correction** — interpolate flagged intervals rather than only deleting, so you
   preserve the successive-difference adjacency RMSSD depends on. If more than ~5% of
   a window is artifact, discard the window rather than trust it.

**Where to compute it:** nocturnal, during sleep, over stable (low-movement) periods.
Whoop weights the estimate toward slow-wave sleep and the later part of the night.
A pragmatic version: take a stable multi-minute window (or several) during deep/core
sleep, clean it, compute RMSSD per window, then take a robust central value (median)
across windows for the night.

**Log-transform for all baseline stats:** RMSSD is right-skewed / roughly log-normal.
Use `lnRMSSD = ln(RMSSD)` for baselines, z-scores, and trend lines. This is standard.

### 1.2 The baseline

Maintain, per metric, a rolling **mean** and **standard deviation** over a trailing window:

| Reference               | Window      | Purpose                                      |
|-------------------------|-------------|----------------------------------------------|
| Long baseline           | 30–60 days  | "Normal range" — the anchor for scoring      |
| Short trend             | 7-day mean  | Where you are right now vs. the anchor        |
| Normal-range band (SWC) | mean ± 0.5·SD | Inside = noise, ignore; outside = meaningful |

- **Smallest Worthwhile Change (SWC) = 0.5 × SD** of the long baseline. This is the
  sports-science convention for "did anything actually change." Very useful for the
  UI: draw the band, and only surface days that break out of it.
- **Coefficient of Variation (CV = SD / mean × 100)** is itself informative. A rising
  CV over a week or two can flag accumulating fatigue even when the mean looks stable.

Commercial anchors: Whoop uses a 30-day baseline; Oura's HRV balance uses ~28 days.
Either is fine. Longer = more stable but slower to adapt.

### 1.3 Inputs and how each behaves

| Metric              | Direction (better recovery)   | Role            | Rough weight |
|---------------------|-------------------------------|-----------------|--------------|
| HRV (lnRMSSD)       | higher than baseline = better | dominant driver | ~60–65%      |
| Resting HR (noct.)  | lower than baseline = better  | secondary       | ~20%         |
| Respiratory rate    | stable; a rise = worse        | flag / minor    | ~15%         |
| SpO2                | dips = worse                  | health flag     | penalty only |
| Body/wrist temp     | deviation (either way) = worse| illness/luteal flag | penalty only |

Notes:
- **Resting HR:** use the nocturnal value, ideally the lowest / late-night point.
  It carries information that overlaps heavily with HRV, which is why it gets less
  weight — it's most useful when it *stops* tracking HRV.
- **Respiratory rate** is remarkably stable night to night, so it's better as a
  threshold flag than a continuous linear input: a rise of ~1 breath/min above
  baseline is a meaningful illness/overreaching signal.
- **Temperature** is best as a deviation-from-baseline penalty. Oura treats a
  deviation beyond roughly ±0.5°C as significant (illness, strain, or luteal phase).
  Don't reward a low deviation; just penalize large ones.
- **SpO2** is noisy on wrist optical sensors — treat as a flag for dips, not a
  continuous contributor.

### 1.4 Combining into 0–100

**Recommended: z-score weighted sum, then squash.** Transparent and driven by your
own variability, so it needs few arbitrary breakpoints.

```
z_hrv  =  (lnRMSSD_today       - mean_lnRMSSD)  / sd_lnRMSSD
z_rhr  = -(RHR_today           - mean_RHR)      / sd_RHR      # inverted
z_rr   = -(RR_today            - mean_RR)       / sd_RR       # inverted, or gate to a threshold

z_total = 0.60*z_hrv + 0.25*z_rhr + 0.15*z_rr

# Squash to 0-100 so that "your average" maps to ~50:
score = 100 * Phi(z_total)          # Phi = standard normal CDF
# or a logistic: score = 100 / (1 + exp(-k * z_total)),  k ~ 1.0
```

Then apply flag penalties on top (subtract a fixed amount when temp deviation > 0.5°C,
SpO2 drops, or RR spikes) — these represent "something is off" states the linear model
shouldn't smooth over.

**Alternative: per-metric sub-scores → weighted average** (same shape as the sleep
score in §2). Map each metric's deviation to a 0–100 sub-score via a piecewise-linear
function, then weight-average. More breakpoints to tune, but easier to explain per-metric
in the UI.

Whoop uses a refinement where, if HRV deviates sharply while RHR stays put, HRV's
weight is temporarily increased. That's a nice v2 idea but almost certainly
over-engineering for a first pass.

### 1.5 On reverse-engineering Athlytic

Athlytic reads the same Apple Health data and produces a recovery %. Under the hood
it's the same recipe: HRV-vs-baseline dominant, RHR secondary, sleep as an input.
You will not reproduce its exact numbers (proprietary weights + its own RMSSD/baseline
choices), but a z-score model over your own baseline will *track* it closely. If you
want to align, log Athlytic's daily score for a few weeks and fit your weights to it —
but tuning to your own subjective feel (see §3) is more valuable long-term.

---

## 2. Sleep score

### 2.1 Inputs from the Health export

- **Stages:** Apple provides Awake, REM, Core (≈ light / N1+N2), Deep (N3).
- **Durations:** total sleep time (TST), time in bed (TIB).
- **Sleep heart rate** series (enables the recovery-index cross-signal in §2.4).

### 2.2 Components and a starting weight set

**Honest caveat first: these weights are heuristic, not scientifically validated.** No
published study fixes them, and the commercial systems keep theirs proprietary (Whoop
and Oura both decline to publish exact weightings; the patent weights are labelled
"exemplary"). What *is* evidence-backed is the *ordering*: total sleep duration and
sleep continuity/regularity have the strongest, most robust links to health and next-day
function; stage *proportions* have weaker independent predictive value once duration is
accounted for — and they are the least reliably measured on a wrist device (see §2.5).
Treat any weight set as a starting point to tune against your own subjective feel.

Reworked set — disturbances split into two components. WASO (total wake time) carries
the weight, because sustained wakefulness is what marks a genuinely disrupted night;
awakening *count* is a lighter penalty, since a few brief arousals with little total
wake time are normal. Funding: WASO inherits most of the old disturbances weight, and
the new awakenings component is topped up by trimming a little from consistency.

| Component                     | Weight | Notes                                                        |
|-------------------------------|--------|--------------------------------------------------------------|
| Duration vs. personal need    | 35     | Largest. Strongest evidence, and well-measured.              |
| WASO (total wake time)        | 20     | Your strongest continuity signal; well-measured (95%+ wake detection). |
| Consistency (timing)          | 17     | Well-measured *and* strongly evidence-backed.                |
| REM                           | 12     | Measured ~70–83% accurately on Apple Watch.                  |
| Awakenings (wake count)       | 8      | Light penalty — brief arousals matter little if WASO is low. |
| Deep                          | 8      | Deliberately below REM — only ~50% accurate.                 |

Three honest notes:
- **WASO ≫ awakenings, on purpose.** Sustained wake time is the disruption you actually
  feel; a handful of brief wake-ups with low total WASO is normal sleep. A measurement
  point reinforces this: brief arousals are exactly what wake-detection tends to miss, so
  awakening *count* is also the noisier of the two numbers. (Caveat the other way: the
  literature credits fragmentation with some independent harm even at low WASO — the
  arousal index — so if fragmented-but-low-WASO nights genuinely feel bad to you, nudge
  awakenings up. This replaces the earlier "take the worse of the two" idea: separate
  weights express the same intent more directly.)
- **Continuity was un-double-counted.** Efficiency (TST/TIB) was dropped earlier because,
  with no latency term, it measured essentially the same wake-during-the-night quantity
  as WASO. Its freed weight lives in WASO, awakenings, and consistency — all of which
  ride on the Watch's most accurate signal (sleep vs. wake) and all well-evidenced.
- **Deep < REM on purpose.** Raising stage weight is physiologically sound, but per §2.5,
  deep sleep is the single least trustworthy number the Watch gives you, so weighting it
  heavily mostly amplifies measurement noise.

### 2.3 Sub-score shapes

Map each to 0–100, then weighted-average:

- **Duration:** 100 at your personal need (plateau), losing a fixed number of points per
  hour of shortfall (default 35/h -- an 8h need scores 7h at 65), with a mild penalty for
  large oversleeping. Deliberately steeper than a proportional ramp: a full hour short is
  a real deficit, not a 12% ding. Learn "need" from your own trailing average.
- **WASO:** inverse ramp on total wake-after-sleep-onset minutes. Full marks for a
  near-unbroken night; scale down as accumulated wake time rises. Shape the curve against
  your own good-vs-bad nights — this is the disturbance dimension you care about.
- **Awakenings:** inverse ramp on the *count* of wake-ups, but a gentle one — a few brief
  arousals are normal, so only penalize meaningfully once the count gets high. Lightly
  weighted, its job is to catch the fragmented-but-low-WASO night that WASO alone misses.
- **REM:** proportion vs. your own typical (~20–25% of TST for adults). Score the
  *deficit* below typical; don't over-reward excess.
- **Deep:** same idea (~13–23% typical), but keep its influence modest given the
  measurement caveat — consider displaying it rather than scoring it heavily.
- **Consistency:** the Sleep Regularity Index (see §2.6), mapped to a 0–100 sub-score —
  compute it once and reuse it here. (An inverted SD of bed/wake times over the trailing
  7 days is a simpler fallback if you defer SRI to a later pass.)

### 2.4 Recovery index (optional cross-signal)

Oura's "recovery index" captures two things from your sleep HR curve: (1) how long
*after* your HR hits its nightly minimum you keep sleeping, and (2) whether that
minimum lands in the first half of the night. Both are computable from your sleep HR
series and tie the sleep score to autonomic recovery — a nice touch given you'll have
the raw HR anyway. Getting the HR low point early and then 6+ hours of sleep after it
is the "good" pattern.

### 2.5 Why stages get modest weight: measurement reliability

This is the real constraint, and it's specific to your hardware. Against
polysomnography, the Apple Watch nails sleep-vs-wake (95%+) but is far weaker at
staging, with asymmetric (biased) errors:

- **Deep sleep ~50–51% sensitivity** — roughly a coin flip — and biased *high* by
  ~43 min/night. A reported 90 min of deep could really be ~45.
- **Light/core ~83–86%**, also overestimated (~45 min/night high).
- **REM ~68–83%** — the most trustworthy of the three stages.

Apple Watch scores best-in-class among wrist wearables (Cohen's κ ≈ 0.53, "moderate"),
and Apple retrained its staging model in late 2025 (watchOS 26) using foundation models
from the Heart and Movement Study — so current accuracy may be somewhat better than
these figures, but there's no solid independent post-update validation yet. Bottom line:
REM is usable as a scored component; deep is best treated as a soft, low-weight signal
or simply displayed rather than scored.

### 2.6 Deeper-dive metrics (detail view)

What Athlytic surfaces when you drill into a night. Three of the four are *derived views*
of data you already store — they need no new ingestion, just a detail screen reading the
nightly metrics table. Only target-sleep and SRI add genuinely new computation.

**Restorative sleep %** = (deep + REM) / TST.
- A repackaging of the two stage components you already score. Fine as a display, with
  two caveats: (1) it inherits the deep-sleep measurement problem — deep is ~50% accurate
  and biased ~43 min high, so restorative % reads systematically inflated and you'll clear
  a fixed "33% = good" bar almost every night, making that threshold nearly useless;
  (2) judge it against *your own* typical restorative %, not the 33% figure — a drop well
  below your personal band is the signal.
- Do **not** also score it. Deep and REM already contribute (8 + 12); scoring restorative %
  on top would double-count them. Display only.

**Target sleep (next night)** — the most useful of the four: forward-looking and
actionable rather than a rear-view scorecard.
- `target = personal_need + min(payback_cap, α · sleep_debt)`
- `personal_need` is partly subjective and can't be cleanly derived from wearable data —
  make it user-set with a nudge from habitual sleep on unconstrained days.
- Cap the debt payback (e.g. +90 min) so a rough week doesn't tell you to sleep 11 hours.
- Optional: raise the target when recovery is low, tying the two scores together.

**Sleep debt** — useful and motivating, and the input target-sleep needs. Frame honestly:
a *loose accounting model*, not physiology. The body doesn't repay sleep hour-for-hour,
and catch-up sleep only partially reverses chronic restriction — so it's directional.
- Rolling window ~14 days (debt older than ~2 weeks isn't meaningfully repayable).
- `debt = Σ over window (personal_need − actual)`, surplus offsets with diminishing
  returns, floor at 0.

**Sleep consistency → use the Sleep Regularity Index (SRI).** Where you can beat Athlytic's
generic consistency readout. Rather than a hand-rolled SD of bed/wake times, compute the
research-grade SRI: the probability you're in the same sleep/wake state at any two points
24 h apart, averaged over 7 days, scored 0–100 (100 = perfectly regular).
- Rides on epoch-by-epoch sleep/wake — the Watch's *most* accurate signal (95%+).
- Unusually well-evidenced: in UK Biobank, the more-regular quintiles had ~20–48% lower
  all-cause mortality than the least regular, and regularity predicted mortality more
  strongly than sleep duration did.
- Compute once, use twice: as the score's Consistency component (map SRI → 0–100 sub-score)
  *and* as the deep-dive display. No double-counting.
- Gotcha: SRI implementations differ enough to change results — fix one convention
  (noon-to-noon anchoring, 7-day rolling window) and keep it stable so history stays
  comparable.

All four sit in the drill-in detail view off the nightly metrics table, so they slot in as
a later pass once the core scores exist.

---

## 3. Practical notes for the hub

- **Warm-up period:** baselines aren't trustworthy until ~14–30 days of data. Mark
  scores provisional before then (Whoop greys out the first 4 days entirely). Don't
  show a confident number off 3 nights.
- **Nightly batch job:** clean RR → compute per-night metrics → update rolling
  baselines → compute scores. Small, sequential, no real-time compute. Store the
  morning digest output alongside.
- **Store raw *and* derived:** keep cleaned RR summaries, per-night metrics, rolling
  baseline snapshots, *and* the final scores. You'll want to retune weights later
  without having thrown away history — recompute scores retroactively from stored raw.
- **Validate against feel:** log a subjective 1–5 recovery rating each morning.
  Correlating it against your computed score is the only real way to tune weights,
  and it's the thing the commercial apps can't do for *you* specifically.
- **Don't over-fit v1:** dynamic weighting, illness detection, menstrual-phase
  correction, etc. are all real but are later passes. A clean z-score recovery model
  and a weighted sleep sub-score, both over a solid baseline, get you ~90% of the value.
- **UI structure and design language:** base the *structure* of the Health tab — its
  information architecture, screen hierarchy, and component patterns (score ring,
  metric cards with baseline deltas, the drill-in detail view for the deeper sleep
  metrics) — on the provided reference screenshots. But render everything in the
  *existing project's design language*: reuse the current dashboard's colour tokens,
  typography, spacing, card/radius conventions (per DESIGN.md and the frontend-design
  plugin). Take the layout and patterns from the screenshots; do not copy their visual
  styling. The Health tab should look like a native part of this hub, not a transplanted
  app.

---

## 4. Suggested build sequence (one clean pass each)

1. **Ingest + store raw** — endpoint + schema for RR/beat-to-beat intervals, sleep
   stages, sleep HR, respiratory rate, SpO2, temperature. Health tab shows raw only.
2. **RR cleaning + nightly RMSSD** — artifact pipeline, lnRMSSD per night.
3. **Baseline table** — rolling mean/SD, 7-day trend + 30–60-day normal range + SWC band.
4. **Recovery score** — z-score model + flag penalties, provisional-until-warmed-up.
5. **Sleep score** — sub-scores + weighted average + (optional) recovery index.
6. **UI + morning digest** — trend charts, today's scores with per-contributor
   breakdown, drill-in detail view for the deeper sleep metrics; fold the overnight
   summary into the existing digest. Structure from the reference screenshots, styling
   from the project's own design language (see §3).
7. **Later** — subjective-feel logging + weight tuning; then habit↔score correlation
   once habit tracking exists.

---

## 5. Source landscape (for your own further reading)

- **Whoop** — recovery = HRV (RMSSD in slow-wave sleep, dominant) + nocturnal RHR +
  respiratory rate, all vs. a 30-day baseline; SpO2/skin-temp as health flags.
- **Oura** — readiness from ~7 weighted contributors: sleep, HRV balance (28-day),
  RHR trend, temperature deviation (±0.5°C threshold), recovery index, activity balance.
- **HRV4Training / Marco Altini** — the most rigorous public writing on doing this
  correctly from wearable/PPG data: lnRMSSD, artifact removal, normal-range/SWC.
- **EliteHRV / sports-science literature** — coefficient of variation, SWC = 0.5·SD,
  7-day rolling averages, and the HRV-guided-training studies (Kiviniemi, Javaloyes).
- **Kubios / Lipponen–Tarvainen (2019)** — the reference method for RR artifact
  detection and correction.
