# Health Module — Build Plan

Incremental build plan for the Health tab. **Work one pass at a time.** After each pass:
implement only that item, report what changed, and stop for review before starting the
next. Do not skip ahead or bundle passes into one diff.

Full specification for every pass lives in `health-scoring-methodology.md` — read the
referenced section before implementing.

Conventions for every pass:
- Keep the Mac thin: storage, serving, light aggregation only. No real-time or heavy compute.
- Match the existing Flask / SQLite / pyserial patterns and device-type conventions.
- Don't touch the serial layer or unrelated device types unless the pass says so.
- Store raw values *and* derived values, so scores can be recomputed if weights change.
- State explicitly what is out of scope for the pass.

---

## Passes

- [x] **1. Ingest + store raw** *(spec: methodology §1.1 field list, §3)*
  `POST /health/ingest` accepting Health Auto Export JSON → SQLite. Tables for: raw
  beat-to-beat RR intervals per night, sleep stages (awake/REM/core/deep with start/end
  times), sleep heart-rate samples, and nightly respiratory rate, SpO2, wrist temperature.
  Validate and reject malformed payloads; store raw, no cleaning or derived metrics.
  Health tab shows the latest night's raw values in a read-only list to confirm ingestion.
  *Out of scope: cleaning, baselines, scores, charts.*

- [x] **2. RR cleaning + nightly RMSSD** *(spec: methodology §1.1)*
  Artifact pipeline over the raw RR series (physiological bounds → relative threshold →
  interpolate; discard windows >~5% artifact). Compute per-night RMSSD over stable
  sleep windows, then `lnRMSSD`. Also derive nightly resting HR (nocturnal low/late) and
  store respiratory rate, SpO2, temperature as clean per-night values.
  *Out of scope: baselines, scoring.*

- [x] **3. Baseline table** *(spec: methodology §1.2)*
  Nightly job maintaining per-metric rolling stats: 7-day trend mean, 30–60-day mean + SD
  (the "normal range" anchor), and the SWC band (mean ± 0.5·SD). Same for lnRMSSD and RHR.
  Mark metrics provisional until ~14–30 days of data exist.
  *Out of scope: scores, UI beyond a raw baseline readout.*

- [x] **4. Recovery score** *(spec: methodology §1.3, §1.4)*
  Z-score model: per-metric z vs. baseline, sign-adjusted (HRV +, RHR −, RR −), weighted
  ~0.60 / 0.25 / 0.15, squashed to 0–100. Flag penalties for temp deviation >0.5°C, SpO2
  dips, RR spikes. Provisional until baselines warmed up.
  *Out of scope: sleep score, UI charts.*

- [x] **5. Sleep score** *(spec: methodology §2.1–2.4)*
  Six weighted sub-scores → 0–100: Duration 35, WASO 20, Consistency 17, REM 12,
  Awakenings 8, Deep 8. Consistency component = SRI (see pass 6a) or the SD fallback if
  SRI is deferred. Optional: recovery index from sleep HR curve.
  *Out of scope: deep-dive metrics, UI.*

- [x] **6. Deeper-dive sleep metrics** *(spec: methodology §2.6)*
  - Restorative sleep % = (deep + REM)/TST — display only, vs. personal baseline. Not scored.
  - Sleep debt — rolling 14-day (need − actual), surplus offsets with diminishing returns.
  - Target sleep next night — need + capped debt payback.
  - Sleep Regularity Index (SRI) — 0–100, noon-to-noon anchor, 7-day window; reused as the
    score's Consistency component from pass 5.
  *Out of scope: UI beyond exposing the values.*

- [x] **7. UI + morning digest** *(spec: methodology §3 UI note, §4 step 6)*
  Health tab: today's recovery + sleep scores with per-contributor breakdown, trend charts,
  and a drill-in detail view for the deeper sleep metrics. Fold the overnight summary into
  the existing morning digest.
  **Structure** from the provided reference screenshots (information architecture, screen
  hierarchy, component patterns). **Styling** from the existing project design language —
  reuse current colour tokens, typography, spacing, card/radius conventions (DESIGN.md /
  frontend-design plugin). Do not copy the screenshots' visual styling; the tab should look
  native to this hub.

- [x] **8. Tuning + correlation (later)** *(spec: methodology §3)*  — subjective
  rating + score correlation done; cross-domain habit↔score correlation deferred
  (blocked: no habit tracking exists yet).
  Subjective 1–5 morning rating; correlate against computed scores to retune weights.
  Then cross-domain habit ↔ score correlation once habit tracking exists.

---

## Progress log

Claude Code: append a one-line note here after each completed pass (what changed, any
deviations from spec), and tick the box above.

- **Pass 1** (2026-07-09): `app/health.py` (self-contained module, 4 raw tables keyed to a
  noon-to-noon "night", `POST /api/health/ingest` + `GET /api/health/latest-night`), Health
  view on the dashboard (read-only raw list), 12 tests. Deviations: endpoint lives under
  `/api/health/ingest` (project prefix convention) not `/health/ingest`; RR beat-to-beat
  intervals aren't a stock Health Auto Export metric, so ingest accepts a documented
  `rr_intervals`/`heartbeat_series` shape (see docs/api.md) — confirm the real exporter's
  format; exact-duplicate rows are dropped for idempotent re-ingest.
- **Passes 2-4** (2026-07-09): pure math in new `app/health_compute.py` (RR clean/RMSSD,
  resting HR, baselines, z-score recovery model), persistence + orchestration in
  `app/health.py`. Three derived tables: `health_night_metrics` (pass 2), `health_baselines`
  (one snapshot per night+metric, pass 3), `health_scores` (score + every intermediate,
  pass 4). Pipeline (clean RR → per-night metrics → rolling baselines → recovery score) runs
  off ingest for the nights whose raw data changed — **not** a timer, since data arrives by
  push (still the thin nightly-batch shape, no new thread). New endpoints
  `GET /api/health/night` (consolidated metrics+baselines+score readout) and
  `POST /api/health/recompute` (rebuild from stored raw after a weight change). Weights/
  windows/penalties are all env vars (`config.HEALTH_*`). 27 new tests (20 pure math + 7
  integration). Deviations/choices: baseline window **includes** the scored night (one night
  barely moves a 30-60d mean; simpler and robust in warm-up); RHR = 5th-percentile of sleep
  HR (nocturnal low); resting/vitals reduced by median; SpO2 fractions auto-normalised to
  percent; provisional flag follows the dominant HRV baseline's warm-up (default 14 nights,
  60-day long window). **No Health-tab UI for these passes** — the tab stays the pass-1 raw
  list; scores/charts are pass 7 per the plan. Everything is exposed via API + tests.
- **Pass 5** (2026-07-09): sleep score — six weighted sub-scores (Duration 35, WASO 20,
  Consistency 17, REM 12, Awakenings 8, Deep 8) → 0-100, plus the optional sleep-HR-curve
  recovery index. Sleep-stage durations added to `health_night_metrics`; score + subscores
  in new `health_sleep_scores`. Personal sleep "need" is a user setting
  (`GET/PUT /api/health/settings`, default 8h). Consistency uses the SD-of-timings fallback
  this pass (SRI in pass 6). REM/Deep scored vs the personal baseline; weights renormalise
  over present sub-scores.
- **Pass 6** (2026-07-09): deep-dive metrics — restorative % (display only), rolling 14-day
  sleep debt (surplus discounted), next-night target sleep (need + capped payback), and the
  Sleep Regularity Index (noon-to-noon, 7-day, epoch-sampled). SRI now drives the Consistency
  sub-score when the window has ≥2 nights (SD fallback otherwise; `consistency_src` records
  which). Surfaced under `sleep` in `GET /api/health/night`.
- **Pass 7** (2026-07-09): Health tab UI + morning digest. Structure from the reference
  screens (two score cards with contributor breakdowns, vitals-vs-baseline, deep-dive tiles,
  stage bars with typical ticks, hypnogram, score trends), rendered entirely in the hub's own
  soldermask/silkscreen language (not the screenshots' styling). New `GET /api/health/history`
  for trends; `morning_snapshot()` folds last night's scores into the existing Sleeping→Day
  digest (a `health` key, like `planner`). Replaced the pass-1 raw list.
- **Pass 8** (2026-07-09): subjective 1-5 morning rating (`POST /api/health/subjective`,
  new `health_subjective` table) + Pearson correlation of rating vs computed scores
  (`GET /api/health/correlation`), surfaced as a "Morning check-in" card. Correlation is the
  tuning signal — weights stay manual (`HEALTH_*` env + recompute), not auto-fitted, per the
  methodology's caution. Habit↔score correlation is **not** built: it depends on a habit
  tracker that doesn't exist yet — a genuinely separate future pass.
