# API notes

Base URL: `http://<host>:8000/api`. All responses are JSON. Timestamps are
unix epoch seconds (UTC); the dashboard formats them client-side.

## Sensors

- `GET /sensors/latest` → `{"temp": {"value": 21.4, "ts": ...}, "hum": ..., ...}`
  — most recent reading per metric (`temp`, `hum`, `lux`, `co2`, `motion`).
- `GET /sensors/history?metric=temp&range=24h` →
  `{"metric": "temp", "points": [{"ts": ..., "value": ...}, ...]}`
  — `range` accepts `<n>m|h|d` (e.g. `30m`, `3h`, `7d`), default `24h`.
  Series are downsampled server-side to ≤ ~300 points by time-bucket
  averaging (bucket **max** for `motion`, so events survive averaging).
- `GET /sensors/profile?metric=temp` →
  `{"metric": ..., "days": 7, "points": [{"tod": ..., "value": ...}, ...]}`
  — the "typical day" curve: 7-day average per half-hour bucket of the day;
  `tod` is seconds since local midnight at the bucket center. The dashboard
  overlays this on the 24h chart and derives the "Typical now" stat from it.
- `GET /sensors/stats?metric=temp` →
  `{"min_24h", "max_24h", "avg_24h", "avg_7d"}` — summary stats for the
  dashboard's expanded widget view. `null` fields mean no data in that window.
- `GET /motion/events?range=24h` → `{"events": [{"ts": ...}, ...], "count": n}`
  newest first (max 50), for the activity log; `count` covers the whole range.

## Devices

Two device types exist so far, both WiFi (never routed through the
Arduino/serial lane): `wifi_plug` (myStrom) and `bulb_zone` (ambient
lighting, a Shelly Multicolor Bulb E27 Gen3 per zone — see "Lighting" below).

- `GET /devices` → array of `{id, name, type, ip, room, ...}`. `room` is seeded
  empty (single-room hub) and kept for a future multi-room one. `wifi_plug`
  rows include `"power"` (last polled `{ts, watts, relay_on}`, or `null`).
  `bulb_zone` rows include `"mode"` (`"manual"|"auto"`) and `"light"` (live
  `{on, brightness, color}`, or `null` if the zone is unreachable).
  One call refreshes every plug widget and lighting card.
- `GET /devices/:id` → same per-type shape as one row above.
- `POST /devices/:id/toggle` → *wifi_plug only.* `{"relay_on": true|false}`
  (new state). `502` with `{"error": ...}` if the plug is unreachable.
- `GET /devices/:id/power/history?range=24h` → *wifi_plug only.*
  `{"device_id": ..., "points": [{"ts", "watts"}, ...]}` — same range/
  downsampling rules as sensor history.
- `GET /devices/:id/power/stats` → *wifi_plug only.*
  `{"avg_24h_w", "kwh_24h", "avg_7d_w"}` — `kwh_24h` is average draw
  integrated over the hours actually covered by samples, so it's an
  estimate (≈) rather than metered energy.

## Lighting (bulb zones)

- `POST /devices/:id/state` → *bulb_zone only.* Body: any subset of
  `{"on": bool, "brightness": 0-255, "ct": 2700-6500, "color": [r, g, b]}`.
  Pushes a partial update to the zone and returns its resulting
  `{on, brightness, ct, color, color_mode}`. `400` on out-of-range values,
  `502` if the zone is unreachable. Only meaningful in `manual` mode — in
  `auto` mode the lighting job (see below) overwrites `on`/`brightness` on
  its next tick.

  **`ct` vs `color` — two channels, one lit at a time.** The bulb has a
  dedicated white channel plus separate R/G/B dies. `ct` (kelvin) drives the
  white channel and is what the dashboard's **Ambient** control sends;
  `color` drives the RGB dies and is what **Custom** sends. `color_mode` in
  the response (`"cct"` or `"rgb"`) says which is currently lit — it is
  deliberately not called `mode`, which on a device row already means
  manual/auto.

  Sending both is a `400`: the winner would otherwise depend on the bulb's
  current mode. A `brightness`-only update touches neither channel, so the
  auto-lighting job never knocks a zone out of ambient white.

  Ambient is CCT because that is where the light is. Measured at full
  brightness on the real bulb: `ct` 2700 K draws **8.7 W**, the equivalent
  warm white mixed from the RGB dies **5.5 W** — and the lumen gap is wider
  than the wattage gap, since RGB dies are much less efficacious per watt
  than a white phosphor.

  The **2700-6500 K range is a hard device limit**, not a UI preference: the
  bulb answers anything outside it with JSON-RPC `-103` rather than clamping.
  The API rejects out-of-range `ct` up front; `shelly_bulb.clamp_ct()` is the
  clamp for internal callers.

  Brightness is **0-255 across the whole hub**. The Shelly bulb's own scale
  is 1-100 %; that conversion happens inside `app/shelly_bulb.py` and
  nowhere else, so nothing above the client ever sees percent. There is no
  `effect` field — the bulb has no effect engine (the numbered effects in
  the API before the Shelly migration were a WLED feature).
- `POST /devices/:id/mode` → *bulb_zone only.* Body: `{"mode": "manual"|"auto"}`.
  In `auto` mode, a background job (independent of sensor ingestion and
  plug polling — see `app/lighting.py`) reads the latest BH1750 `lux`
  reading every `LIGHTING_POLL_INTERVAL` seconds and pushes brightness to
  the zone: below `LIGHTING_LUX_THRESHOLD` it turns on to
  `LIGHTING_AUTO_BRIGHTNESS`; at/above it, it turns the zone off. All three
  are env vars (`.env`), not hardcoded.

## Scenes (house modes)

Named multi-device states — seeded: `Sleeping`, `Day`, `Away` (definitions
in `app/db.py` `SCENE_SEEDS`, stored per-row in the `scenes` table and
editable there). While a scene other than `Day` is active, the auto-lighting
job is suppressed; zones keep their `mode` column, so `Day` resumes lux
control on any zone still set to `auto`. The active scene persists across
backend restarts.

- `GET /scenes` → array of `{id, name, states}`. `states` keys are the group
  keys `all_plugs`/`all_zones` (target applies to every device of that type,
  present and future) and/or device *names* (overriding the group's fields
  for that device); each value is a partial target
  (`{"on": ..., "brightness": ..., "color": ...}`, fields/devices not
  covered are left alone).
- `POST /scenes/:name/activate` (name is case-insensitive) → applies every
  target and persists the scene. Optional body — only when activating
  Sleeping: `{"wake_time": "HH:MM"}` (local time) schedules an automatic
  switch back to Day at that time. **A scene change only, not an alarm** —
  no sound, no notification. Blank/absent = stay in Sleeping until changed
  manually. Any activation cancels a pending wake; a pending wake survives
  restarts (overdue ones fire at startup). Response:
  `{"active": {name, activated_at, wake_time, wake_at}, "devices": [...],
  "summary_generated": bool}` — `devices` reports per-device success;
  locked plugs are skipped (`{"ok": false, "skipped": "locked"}`) and an
  unreachable device never blocks the rest. `400` on a bad `wake_time`,
  `404` for an unknown scene.
- `GET /scenes/active` → `{name, activated_at, wake_time, wake_at}` —
  defaults to `Day` (null timestamps) if nothing was ever activated.
- `GET /scenes/last-summary` → `{"summary": null}` or the most recent
  Sleeping→Day overnight summary, computed at the transition from existing
  readings over the Sleeping window:
  `{"from", "to", "temp": {min,max,avg}, "hum": {min,max,avg},
  "co2": {avg, start, end, delta, rose_significantly}, "motion": {count, events},
  "planner": {events, tasks}}`
  (`co2.avg` is the headline number — the delta can legitimately be
  negative; `rose_significantly` = CO₂ climbed ≥ 200 ppm start→end; null
  fields mean no data in the window). `planner` is the day-ahead half of the
  same summary: today's events (`{title, start, end, all_day}`, expanded
  occurrences, max 10) and open overdue/high-priority tasks
  (`{id, title, due_date, priority, overdue}`, max 10) — snapshotted at the
  transition like the sensor stats. Summaries stored before the planner
  existed have no `planner` key.

## Planner (calendar + to-do)

A self-contained module (`app/planner.py` — own tables, own blueprint);
nothing here touches the device lanes. Event `start`/`end` are epoch seconds
in responses; write endpoints also accept local ISO strings —
`"YYYY-MM-DDTHH:MM"` (a `datetime-local` input) or `"YYYY-MM-DD"` (a `date`
input, for all-day). Task `due_date` is a plain `"YYYY-MM-DD"` string. Both
tables reserve an unused `external_uid` column so a future CalDAV sync layer
(e.g. Radicale) can map its UIDs onto rows without a schema rewrite.

`all_day` is an explicit boolean (the iCal DATE-vs-DATE-TIME split). For an
all-day event the stored `start` is local midnight and `end`, when set, is the
**exclusive** midnight after the last covered day (so a Jul 9–11 event stores
`end` = midnight Jul 12); `end: null` is a single day. Timed events may still
span multiple days (a real start and end datetime on different days) — that is
distinct from all-day.

- `GET /events?from=2026-07-05&range=7d` → `{"from", "to", "events": [...]}`
  — events intersecting the window from local midnight of `from` (default
  today) running `range` days (default `7d`, max `366d`). Recurring events
  (`recurrence`: `none`/`daily`/`weekly` — deliberately not RFC 5545) are
  expanded into per-occurrence entries at the series' local wall-clock time
  (DST-safe; all-day spans are measured in whole days, so a multi-day span
  survives DST). Each entry carries the row's `id`, `category`, `all_day`,
  plus `series_start`/`series_end` (the stored definition, for edit forms).
- `POST /events` → `201` + the row. Body:
  `{"title", "start", "end"?, "notes"?, "recurrence"?, "category"?, "all_day"?}`;
  `category` is one of the predefined set `home`/`work`/`personal`/`health`/
  `social` (or null — see `CATEGORIES` in `app/planner.py`). With
  `all_day: true` the bounds are snapped to whole local days (end exclusive);
  otherwise `end <= start` and unknown categories are rejected with 400.
- `PUT /events/:id` → partial update, any subset of the POST fields
  (`"end": null` clears the end). Toggling `all_day` re-snaps the (possibly
  unchanged) bounds to whole days or back. Editing a recurring event edits the
  whole series — occurrences are derived, never stored.
- `DELETE /events/:id` → `{"deleted": id}`.
- `GET /tasks?list=home&done=false` → `{"tasks": [...]}`, each
  `{id, title, due_date, priority, done, list, created_at, completed_at}`.
  Both filters optional; sorted open-first, then due date (no due date
  last), `priority` (`low`/`medium`/`high`), age.
- `POST /tasks` → `201` + the row. Body:
  `{"title", "due_date"?, "priority"?, "list"?}` (priority defaults to
  `medium`; `list` is a free grouping tag like "home"/"work").
- `PUT /tasks/:id` → partial update, any subset of the POST fields plus
  `"done": bool` — completing sets `completed_at`, reopening clears it.
- `POST /tasks/:id/complete` → one-tap complete; idempotent (a second call
  keeps the original `completed_at`).
- `DELETE /tasks/:id` → `{"deleted": id}`.

## Health (sleep / recovery)

A self-contained module (`app/health.py` — own tables, own blueprint under
`/api/health`); nothing here touches the device lanes. Build pass 1 of
`docs/health-build-plan.md`: **raw storage only** — no cleaning, baselines,
or scores yet. Every stored row is keyed to a `night`: the local wake-morning
date, assigned noon-to-noon (a sample between 12:00 on day D and 12:00 on
day D+1 belongs to night `D+1`).

- `POST /health/ingest` — Health Auto Export ("JSON + CSV" iOS app) REST
  push: `{"data": {"metrics": [{"name", "units", "data": [...]}, ...]}}`
  (a bare `{"metrics": [...]}` also works). Recognized metrics:
  - `sleep_analysis` — stage **segments** `{"startDate", "endDate",
    "value": "Awake"|"REM"|"Core"|"Deep"}`; requires "aggregate sleep data"
    **off** in the app (aggregate rows → 400 with a pointed error).
    `In Bed`/`Asleep` rows (stage-less sources) are skipped.
  - `heart_rate` — `{"date", "Avg"}` or `{"date", "qty"}` → bpm samples.
  - `respiratory_rate`, `blood_oxygen_saturation`,
    `apple_sleeping_wrist_temperature` — `{"date", "qty"}` → nightly vitals
    samples (`resp_rate` / `spo2` / `wrist_temp`).
  - `rr_intervals` (alias `heartbeat_series`) — beat-to-beat RR intervals in
    **milliseconds**, either `{"date", "qty"}` (one interval per item) or
    `{"date", "intervals": [ms, ...]}` (a run starting at `date`; each
    interval's timestamp advances by the ones before it). Not a stock
    Health Auto Export metric — whatever exports your heartbeat series must
    produce one of these two shapes.

  Date strings accept the Health Auto Export format
  (`"2026-07-08 23:12:00 +0200"`), ISO strings, or epoch seconds. Response:
  `{"stored": {<category>: n, ...}, "duplicates": n, "ignored": [names]}`.
  Unknown metric names are ignored (and reported), never an error. Malformed
  payloads → 400 and **nothing** is stored (single transaction). Re-sending
  an overlapping export is idempotent — exact duplicate rows are skipped and
  counted in `duplicates`.
- `GET /health/latest-night` → the most recent night's raw values, for the
  Health tab's read-only ingest-confirmation list: `{"night": "YYYY-MM-DD",
  "rr": {count, first_ts, last_ts}, "sleep_hr": {count, first_ts, last_ts,
  min_bpm, max_bpm}, "sleep_stages": [{stage, start, end}, ...], "samples":
  {"resp_rate": [{ts, value}], "spo2": [...], "wrist_temp": [...]}}`.
  `{"night": null}` until the first ingest.

Passes 2-4 add the **derived pipeline** (clean RR → per-night metrics →
rolling baselines → recovery score). It runs automatically after ingest for
the nights whose raw data changed (`ingest`'s response gains a
`"recomputed": [nights]` field); raw stays the source of truth, so a weight
change just recomputes. Math lives in `app/health_compute.py`; weights,
baseline windows and penalties are env vars (`config.HEALTH_*`).

- `GET /health/night?night=YYYY-MM-DD` → consolidated derived readout for one
  night (default the latest computed). `{"night", "metrics", "baselines",
  "score"}`:
  - `metrics` — `{rmssd, ln_rmssd, rr_artifact_pct, rr_windows, rhr,
    resp_rate, spo2, wrist_temp}` (each `null` if that input was absent).
    RMSSD is the median over cleaned deep/core sleep windows; `rhr` is the
    nocturnal 5th-percentile sleep HR; vitals are nightly medians; SpO2 is
    normalised to a percent.
  - `baselines` — per metric (`ln_rmssd`/`rhr`/`resp_rate`/`wrist_temp`/
    `spo2`): `{mean, sd, trend_7, swc_low, swc_high, cv, n, provisional}`.
    `mean`/`sd` are the 30–60-day anchor, `trend_7` the 7-day mean, the SWC
    band is `mean ± 0.5·SD`; `provisional` is true until ~14 nights exist.
  - `score` — `{recovery (0–100), base_score, z_total, contributions:
    {hrv|rhr|rr: {z, weight, contribution}}, flags:
    [temp_deviation|spo2_dip|rr_spike], penalty, provisional}`. Z-score model
    (HRV+, RHR−, RR−) squashed with the normal CDF so an average night ≈ 50,
    then flag penalties subtracted. `null` before the night is computed.
- `POST /health/recompute` (`?night=YYYY-MM-DD` optional) → recompute derived
  metrics/baselines/scores from stored raw: with `night`, that night forward;
  without, every night (use after changing `HEALTH_*` weights). Never touches
  raw rows. `{"recomputed_nights": n}`.

`GET /health/night` also carries the **sleep score** (passes 5-6), the raw
stage `stages` (for the hypnogram), and the night's `subjective` rating:

- `sleep` → `{sleep_score (0-100), subscores: {duration|waso|consistency|rem|
  awakenings|deep: {value, weight}}, recovery_index, consistency_src
  ("sri"|"sd_fallback"), restorative_pct, sleep_debt_min, target_sleep_min,
  sri, provisional}`. Six weighted sub-scores (Duration 35 / WASO 20 /
  Consistency 17 / REM 12 / Awakenings 8 / Deep 8) averaged; Consistency is the
  SRI when the 7-day window has ≥ 2 nights, else an inverted SD of bed/wake
  times. Deep-dive values are display-only (not re-scored). `null` if the night
  has no scored sleep.
- `metrics` gains the stage durations (`tst_min`, `waso_min`, `awakenings`,
  `rem_min`/`deep_min`/`core_min`, `rem_frac`/`deep_frac`, `onset_ts`,
  `wake_ts`); `stages` is the raw `[{stage, start, end}]` for that night;
  `subjective` is `{rating (1-5), note}` or `null`.
- `GET/PUT /health/settings` → the user-owned personal sleep need in minutes:
  `{"sleep_need_min": <n>}`. PUT (60–900) recomputes every sleep score against
  the new need; raw untouched.
- `GET /health/history?range=7d|30d|60d` (default `30d`) → per-night scores +
  driver metrics for the trend charts and the dashboard's sleep-detail /
  vitals-history dialogs: `{range, nights: [{night, recovery, sleep_score,
  rmssd, rhr, resp_rate, spo2, wrist_temp, tst_min, waso_min, rem_min,
  deep_min, onset_ts, wake_ts, sri, restorative_pct, sleep_debt_min,
  target_sleep_min, rec_provisional, sleep_provisional}]}`.
- `POST /health/subjective` → log the morning's subjective feel. Body
  `{"rating": 1-5, "night"?: "YYYY-MM-DD", "note"?}` (default: the night just
  woken from). Upsert. Never affects the computed scores — it's the ground
  truth they're correlated against.
- `GET /health/correlation` → Pearson r of the subjective rating vs each
  computed score over all rated nights: `{ratings, recovery: {r, n}, sleep:
  {r, n}}`. `r` is `null` until ≥ 3 rated nights (or if a series has no
  variance). This is the signal for hand-tuning `HEALTH_*` weights to your own
  feel — the hub does not auto-fit them.

## Settings

- `GET /settings/thresholds` → alert thresholds per key
  (`temp`, `hum`, `lux`, `co2`, `power`), each `{"min": ..., "max": ...}`
  with `null` meaning that bound is disabled. Defaults: temp 17–26 °C,
  hum 30–60 %RH, CO₂ max 1000 ppm, plug power max 1800 W, lux off.
- `PUT /settings/thresholds` → replace them (same shape as the GET; the
  dashboard's gear dialog uses this). Rejects `min >= max` with 400.
  A reading outside its range flags the widget with a HIGH/LOW chip, and
  the detail charts shade the out-of-range region as a faint red zone.
- `GET /settings/lighting` → `{"lux_off": 50.0}` — the ambient level at which
  a zone in `auto` mode is fully off. Brightness ramps **linearly** from
  `LIGHTING_AUTO_BRIGHTNESS` at pitch dark down to nothing there, so this one
  number sets the whole curve. Seeded from `LIGHTING_LUX_THRESHOLD`.
- `PUT /settings/lighting` → same shape. `0` is allowed and means "never light
  up from lux"; negative or non-numeric is 400. Pokes the lighting job so the
  change lands on the next tick rather than up to an interval later.
- `GET /settings/sleep-schedule` →
  `{"enabled": true, "sleep_time": "00:00", "wake_time": "09:30"}` — the
  recurring nightly Sleeping window, local "HH:MM".
- `PUT /settings/sleep-schedule` → same shape; re-arms the schedule
  immediately. Non-`HH:MM` times are 400. Switching Sleeping on and off is a
  scene change only — never an alarm, no sound or notification.

## Wired lane passthrough

- `POST /arduino/command`, body `{"command": "RELAY1:ON"}` — writes one raw
  protocol line to the Arduino (see `serial-protocol.md`). `502` if the
  serial port isn't connected. Exists for any future wired actuator (relay,
  MOSFET dimmer) and manual testing. Ambient lighting (bulb zones, above)
  does **not** use this — it's a WiFi device controlled directly, same lane
  as the myStrom plugs.

- `GET /arduino/serial` → `{"paused": false}` — whether the reader currently
  holds the serial port.

- `POST /arduino/serial`, body `{"paused": true, "minutes": 20}` — release the
  serial port so the board can be reflashed (e.g. swapping in
  `firmware/scd40_calibrate`), without stopping the hub: lighting, scenes, the
  planner and health keep running, only sensor ingest stops. Returns
  `{"paused": true, "port_released": true, "resumes_in_minutes": 20}`;
  `port_released` is false if the port was somehow still open ~4s later, which
  means an upload would likely fail. `minutes` defaults to 15 and is capped at
  2 hours — the pause **always** expires on its own, so a forgotten one can't
  leave an unattended hub permanently mute. `{"paused": false}` resumes
  immediately (idempotent).

## Sensor plausibility bands

Readings outside a physically plausible band are **logged and then stored
anyway** (logging throttled to once a minute per metric). The band is advisory:
ingest never drops a reading, so a faulty sensor is visible on the dashboard
instead of looking like one that has gone quiet.

| metric | accepted | why |
|---|---|---|
| `temp` | -40 – 85 | BME280 operating range |
| `hum` | 0 – 100 | |
| `lux` | 0 – 120000 | BH1750 saturates well below this |
| `co2` | 300 – 10000 | real air floors out ~420; **0 = invalid channel** |
| `motion` | 0 – 1 | |

A failing sensor doesn't go quiet, it emits in-protocol nonsense — the SCD4x
reports `CO2:0` when its CO2 channel is invalid. Those rows **do** skew
averages, alert bands, the "typical day" profile and the overnight CO2 delta
for as long as the fault lasts; that is accepted deliberately, because the
alternative hid the fault from the one screen used to diagnose it. See
`METRIC_RANGE` in `app/serial_reader.py`.

## Errors

Failures return `{"error": "<human-readable message>"}` with 400 (bad
params), 404 (unknown device/metric), or 502 (hardware unreachable). The
backend never fakes success — if the plug or serial port is down, callers
hear about it.
