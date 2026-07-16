# CLAUDE.md — Smart Home Hub

This file gives Claude Code persistent context for this repository. Read it before making
architectural decisions. It reflects real hardware/software choices already made — don't
relitigate them without a clear reason.

## Project summary

A DIY smart home hub. An old MacBook (8GB RAM) runs 24/7 as the central server, sensor
database, and web dashboard. An Arduino Due handles all wired, time-sensitive I/O over a
single USB serial connection. WiFi devices — currently two myStrom smart plugs and two
WLED ambient-lighting zones — are controlled directly by the host over the network — no
cloud, no third-party hub, no Home Assistant.

**Hard constraint:** the host is an 8GB RAM MacBook. Never introduce video transcoding,
AI/camera vision, Docker-heavy stacks, or anything with a large idle memory footprint.
Everything on the host should be lightweight I/O: read serial, write SQLite, serve HTTP,
call REST APIs.

## Two device lanes — do not blur these

1. **WIRED lane** (Arduino Due ↔ Mac via USB serial)
   All sensors and any relay/MOSFET/LED-strip actuators wire into the Arduino via
   breadboard — never directly into the Mac. Time-sensitive or per-pixel timing logic
   (e.g. NeoPixel animation) lives entirely on the Arduino. The host only ever sends
   short, high-level text commands over serial and reads short structured lines back.
   Never send per-pixel or per-frame data over serial.

2. **WIRELESS lane** (Mac ↔ WiFi devices directly, bypassing the Arduino entirely)
   The host's server calls each WiFi device's local REST API directly over the LAN.
   Currently two device types:
   - Two **myStrom WiFi Switch** plugs (Swiss Type J, local REST API, power monitoring
     2–3680W) — "Plug 1" (Living Room) is physically installed, "Plug 2" is seeded in
     the `devices` table as a placeholder IP until it's physically installed.
   - Two **WLED ambient-lighting zones** ("Cupboard", "Table") — each a separate ESP32
     running stock WLED firmware (local JSON API, no cloud), driving an addressable LED
     strip near that zone. Both are seeded as placeholder IPs; no ESP32 is flashed yet.
   No cloud account or app is required for runtime control, only initial WiFi
   provisioning. Devices are modeled generically (see below) so further WiFi plugs or
   lighting zones can register without rewriting existing device logic. **Do not confuse
   this with the wired NeoPixel strip in the hardware inventory below** — that would be
   a strip wired directly into the Arduino, driven over serial; WLED zones are wireless
   ESP32 nodes and never touch the Arduino or the serial protocol.

## Hardware inventory (wired / Arduino side)

I2C bus, shared on the Due's SDA/SCL pins (20/21), no address conflicts:

| Sensor | Purpose | Interface | Address |
|---|---|---|---|
| BME280 | Temperature + humidity | I2C | 0x76 or 0x77 |
| BH1750 | Ambient light level | I2C | 0x23 |
| SCD40 (SCD30 also supported) | CO2 | I2C | 0x62 (SCD30: 0x61) |
| HC-SR501 (PIR) | Motion | Digital pin | — |

Use Adafruit-style breakout boards (onboard regulation) — never bare sensor chips. The
Due is **3.3V logic and its pins are NOT 5V tolerant**: power the I2C breakouts from the
3.3V pin so the bus stays at 3.3V. Exception: the HC-SR501 is fed from the 5V pin (its
regulator needs it) but its output signal is natively 3.3V, so it's safe on a Due input.

Planned/future wired additions (design for extensibility, don't build yet):
- WS2812B/NeoPixel strip wired directly to the Arduino (CO2 traffic-light indicator,
  motion accent lighting, sunrise alarm) via `MODE:`/`COLOR:` serial commands — distinct
  from the WLED ambient-lighting zones, which are wireless ESP32 nodes on the WIRELESS
  lane above, not this strip (3.3V data from the Due is usually fine on a short run;
  add a 74AHCT125 buffer if it glitches)
- Opto-isolated relay module for mains ON/OFF switching (must trigger at 3.3V)
- Logic-level MOSFET for low-voltage LED dimming (needs a true 3.3V-gate part — the
  IRLZ44N originally planned is marginal at a 3.3V gate)
- OLED/e-ink display, RFID reader, water leak sensor, soil moisture sensor

## Serial protocol (host ⇄ Arduino)

Line-based, human-readable, terminated with `\n`. Parse with `Serial.readStringUntil('\n')`
on the Arduino side. Keep this protocol stable — both firmware and host code depend on it.

**Host → Arduino (commands):**
```
RELAY1:ON
RELAY1:OFF
DIM1:180                # PWM value 0-255
MODE:aqi                # NeoPixel mode select
COLOR:255,0,0           # NeoPixel direct color set
```

**Arduino → Host (telemetry, sent periodically, one reading per line):**
```
TEMP:21.4
HUM:47.2
LUX:312
CO2:612
MOTION:1
```

Prefer simple `KEY:VALUE` framing over JSON on the wire — it keeps the firmware trivial
and the seam debuggable by eye, and lets a small-RAM AVR node (the original Uno target)
join later without a protocol change. The host is responsible for structuring/labeling
data before it hits the database or API layer.

## WiFi plug integration (myStrom)

- Local REST API only — no myStrom cloud calls at runtime.
- Host needs: device local IP (static DHCP reservation recommended), and knowledge of the
  myStrom local HTTP endpoints for state, toggle, and power-consumption reporting.
- Design the backend so plug polling (state + power draw) runs on its own lightweight
  interval, independent of the Arduino serial read loop — these are two unrelated event
  sources and should not block each other.
- Model devices generically in the backend (e.g. a `devices` table/interface with type
  `wifi_plug`, `ip`, `name`, `room`) so additional WiFi devices can register without new
  one-off code paths.
- Physical setup (provisioning the plug onto WiFi, assigning a static IP) happens later
  and is out of scope for the software work — but the backend, database schema, and API
  should already assume at least one WiFi plug exists and expose endpoints for it (state,
  toggle, power history) from day one.

## WLED lighting integration

- Each ambient-lighting zone is a separate ESP32 running stock **WLED** firmware
  (open source, local JSON API at `/json/state`, no cloud) — same device abstraction as
  the myStrom plug, `type = 'wled_zone'`, with an added `mode` column: `manual` or `auto`.
- `app/wled.py` is the client (get state; push a partial on/brightness/color/effect
  update) — mirrors `app/mystrom.py`'s shape, including a mock for `MOCK_HARDWARE=1`.
- `app/lighting.py` is a **separate** background thread (not the plug poller, not the
  serial reader) that, on its own interval (`LIGHTING_POLL_INTERVAL`), pushes a
  brightness update to every zone currently in `auto` mode based on the latest BH1750
  `lux` reading already in the DB: below `LIGHTING_LUX_THRESHOLD` it turns the zone on to
  `LIGHTING_AUTO_BRIGHTNESS`; at/above it, it turns the zone off. All three are env vars,
  not hardcoded. Zones in `manual` mode are left alone — the dashboard drives those
  directly via `POST /api/devices/:id/state`.
- Physical setup (flashing WLED, wiring the strip, static IP) happens later — the
  backend, database schema, and API already assume both zones exist from day one, same
  as the myStrom plug.

## House modes (scenes)

- A scene is a named, manually-triggered state that sets multiple devices at once
  (implemented in `app/scenes.py`). The `scenes` table holds `id`, `name`, and
  `states` — JSON keyed by the group keys `all_plugs`/`all_zones` (every device
  of that type, present and future) and/or a device *name*, which overrides the
  group's fields for that one device. Each value is a partial target
  (`on`/`brightness`/`color` as applicable); fields a scene doesn't mention, and
  devices no key covers, are left alone. Three seeded scenes (insert-if-missing,
  so hand edits to the rows survive restarts; rows still exactly on an earlier
  seed revision are migrated at startup):
  - **Sleeping** — every LED zone off, every unlocked plug off.
  - **Day** — every unlocked plug on; deliberately *no* zone targets (see
    suppression below).
  - **Away** — every LED zone off, every unlocked plug off.
  (Sleeping and Away share device targets; they differ in the wake-time
  scheduling and morning summary that only Sleeping carries.)
- The active scene (name + activation timestamp + pending wake time) persists in
  the `settings` table so it survives backend restarts. Never-activated counts
  as "Day" — normal operation.
- **Auto-lighting suppression:** while any scene other than "Day" is active, the
  lux-based auto job in `app/lighting.py` is paused wholesale — the scene's
  explicit values win. A scene never rewrites a zone's `mode` column, so
  returning to "Day" resumes lux control on any zone still set to `auto`
  (activation pokes the lighting job so it reacts immediately, not a tick later),
  and `manual` zones stay wherever the scene/user left them.
- Locked plugs are never switched by a scene — skipped and reported per-device in
  the activation response, same protection as the dashboard toggle. One
  unreachable device never blocks the rest of a scene.
- **Wake time (Sleeping → Day):** activating Sleeping accepts an optional
  `wake_time` ("HH:MM", local). A plain in-process `threading.Timer` (no
  job-queue dependency) then switches the scene to Day at that time. It ONLY
  switches the scene — explicitly not an alarm: no sound, no notification.
  Blank/absent means Sleeping holds until changed manually. Any scene activation
  cancels the pending timer (a generation counter makes a stale timer that
  already started firing a no-op). The pending wake is persisted with the active
  scene and re-armed at startup; one that came due while the backend was down
  fires immediately.
- **Morning summary:** every Sleeping → Day transition (scheduled or manual)
  computes overnight stats from the existing `readings` table over the Sleeping
  window — temp/hum min/max/avg, CO2 average plus start vs end (flagged if it
  climbed ≥ 200 ppm; the dashboard headlines the average, since a signed delta
  up front reads like a negative CO2 level), motion count + event times — and
  stores the result in `settings` for `GET /api/scenes/last-summary`. Computed
  once at the transition, no new report system or table.
- Scene behavior is covered by `server/tests/` (stdlib unittest, runs fully
  under `MOCK_HARDWARE=1`): `cd server && python3 -m unittest discover -s tests`.

## Planner (calendar + to-do)

- A **self-contained module**, `app/planner.py`: its own tables (`events`,
  `tasks` — the DDL and `init_db()` live in that file, not in `db.py`) and its
  own blueprint (`/api/events`, `/api/tasks`). It never touches the
  device/scene lanes; the one outward edge is `scenes.py` calling
  `planner.morning_snapshot()` while building the overnight summary.
- Events: `title`, `start`/`end` as epoch seconds (write endpoints also accept
  local `"YYYY-MM-DDTHH:MM"` or `"YYYY-MM-DD"` strings; `end` nullable),
  `notes`, `recurrence` `none|daily|weekly` — deliberately **not** RFC 5545.
  Occurrences are expanded on read at the series' local wall-clock time
  (DST-safe) and never stored, so editing/deleting a recurring event affects
  the whole series.
- `all_day` is an explicit boolean column (the iCal DATE-vs-DATE-TIME split, so
  a future CalDAV layer maps cleanly — **not** the old "midnight start, no
  end" heuristic, which a one-time migration grandfathers into the flag). For
  an all-day event `start` is floored to local midnight and `end`, when set, is
  the **exclusive** midnight after the last covered day (`end` null = single
  day); the length is measured in whole days so a multi-day span survives DST.
  A **timed** event can also span several days (real start/end datetimes on
  different days) — that's separate from all-day, and the calendar clips such
  an event into each day column it touches.
- Event `category` is one of a **fixed, predefined set** — `home | work |
  personal | health | social` (or null) — that the calendar colors with the
  validated series hues; not user-defined tags. The set lives as `CATEGORIES`
  in both `app/planner.py` and `dashboard/app.js` — keep them in sync.
- Tasks: `title`, `due_date` (plain `"YYYY-MM-DD"`, nullable), `priority`
  `low|medium|high`, `done` with `created_at`/`completed_at`, optional `list`
  grouping tag ("home"/"work"). `POST /api/tasks/:id/complete` is the one-tap
  path (idempotent).
- Both tables carry an unused `external_uid` column, reserved so a future
  CalDAV sync layer (e.g. Radicale) can map external UIDs onto rows without a
  schema rewrite — keep any new planner fields similarly plain.
- **Morning summary integration:** the Sleeping→Day summary embeds
  `planner.morning_snapshot()` under a `planner` key — today's events plus
  open overdue/high-priority tasks (each capped at 10), snapshotted once at
  the transition. One summary, not a second report system; summaries stored
  before the planner existed simply lack the key, and the dashboard hides the
  section.
- Covered by `server/tests/test_planner.py` (same mock-hardware harness).

## Health (sleep/recovery)

- Built pass-by-pass per `docs/health-build-plan.md` (spec:
  `docs/health-scoring-methodology.md`) — passes 1–8 done; the only deferred
  item is habit↔score correlation (blocked on a habit tracker that doesn't
  exist). A **self-contained module**, `app/health.py`: its own tables (own
  DDL/`init_db()`, not in `db.py`), own blueprint under `/api/health`. Never
  touches the device/scene lanes (one outward edge: `scenes.py` calls
  `health.morning_snapshot()` for the overnight digest, like it does the planner).
- Two files: **`app/health_compute.py`** is pure math (RR artifact cleaning,
  RMSSD/lnRMSSD, resting HR, rolling baselines, the z-score recovery model) —
  no DB/Flask, so it unit-tests cheaply. **`app/health.py`** owns ingest,
  persistence, and endpoints.
- Data arrives by **push**: the Health Auto Export iOS app POSTs Apple Health
  JSON to `POST /api/health/ingest`. Recognized metrics: sleep stages, sleep
  HR, nightly resp-rate/SpO2/wrist-temp, and beat-to-beat RR intervals (RR is
  not a stock export metric — see the documented shape in `docs/api.md`).
- **Raw is the source of truth, kept forever.** Four raw tables
  (`health_rr`, `health_sleep_stages`, `health_sleep_hr`,
  `health_night_samples`), every row keyed to a **noon-to-noon "night"** (the
  local wake-morning date — a sample from 12:00 day D to 12:00 D+1 is night
  D+1; same anchor SRI will use). Malformed payloads are rejected whole (one
  transaction); re-ingest is idempotent via UNIQUE indexes.
- **Derived pipeline** (clean RR → per-night metrics → rolling baselines →
  recovery score + sleep score + deep-dive) writes derived tables
  (`health_night_metrics` incl. sleep-stage durations, `health_baselines` one
  snapshot per night+metric, `health_scores`, `health_sleep_scores`,
  `health_subjective`). It runs **off ingest** for the nights whose raw data
  changed — not a background thread/timer, since data is pushed — keeping the
  thin nightly-batch shape. `recompute()` / `POST /api/health/recompute` rebuild
  from stored raw so weights can be retuned without re-ingesting; all weights,
  baseline windows, sleep sub-score weights, and penalties are env vars
  (`config.HEALTH_*`).
- **Sleep score** is six weighted sub-scores (Duration/WASO/Consistency/REM/
  Awakenings/Deep); Consistency = SRI when the window allows, else an SD-of-
  timings fallback. Deep-dive values (restorative %, sleep debt, target sleep,
  SRI) are display-only, not re-scored. Personal sleep "need" is a user setting.
- **Tuning is manual, never auto-fitted.** A subjective 1–5 morning rating is
  correlated (Pearson) against the computed scores; the correlation is shown so
  the `HEALTH_*` weights can be hand-tuned to the user's own feel.
- Score against the **user's own rolling baseline**, never population norms
  (methodology §0). Scores are marked provisional until the baseline warms up
  (~14 nights).
- Covered by `server/tests/test_health.py` (ingest + pipeline integration) and
  `server/tests/test_health_compute.py` (pure math), same mock-hardware harness.

## Host stack

- Server: Python/Flask (lightweight, easy serial + REST integration, low idle footprint
  on 8GB RAM — preferred over Node/Express for this project unless you have a strong
  reason to deviate).
- Serial: `pyserial` for Arduino communication over USB.
- Database: SQLite for sensor time-series data and device state history.
- Remote access: Tailscale (assume it's available; don't build custom auth/tunneling).
- Storage: sensor DB, backups, and file sharing live on an already-mounted 1TB USB-C SSD
  — don't assume the DB has to live on the internal drive.
- No MQTT broker yet — single Arduino node for now. Keep the device/data layer decoupled
  enough that adding Mosquitto later for multi-room nodes doesn't require a rewrite.
- `MOCK_HARDWARE=1` env var (see `.env.example`) swaps the serial reader, the myStrom
  client, and the WLED client for fake data generators (plausible sinusoidal sensor
  drift, wobbling plug wattage, in-memory zone state) so the dashboard is developable
  end-to-end without any hardware attached. Keep this mode working when touching
  `serial_reader.py`, `mystrom.py`, or `wled.py`.

## API shape (backend)

Implemented in `server/app/api.py` (planner endpoints in `server/app/planner.py`) —
keep this list in sync when endpoints change:

- `GET /api/sensors/latest` — most recent reading per sensor
- `GET /api/sensors/history?metric=temp&range=24h` — time series for charts (range: `30m`/`24h`/`7d` style)
- `GET /api/sensors/stats?metric=temp` — 24h min/max/avg + 7d avg, for a widget's expanded view
- `GET /api/sensors/profile?metric=temp&bucket=30` — "typical day" curve: 7-day average per time-of-day bucket
- `GET /api/motion/events?range=24h` — recent motion detections + count, for the activity log
- `GET /api/devices` — list known devices (two myStrom plugs, two WLED zones); plug rows carry `power` (last polled sample), WLED rows carry `mode` and `light` (live state)
- `GET /api/devices/:id` — device row with the same per-type fields as above
- `POST /api/devices/:id/toggle` — turn a WiFi plug on/off
- `GET /api/devices/:id/power/stats` — 24h/7d average draw + estimated 24h kWh
- `GET /api/devices/:id/power/history` — power draw over time
- `POST /api/devices/:id/state` — set a WLED zone's on/brightness/color/effect (any subset)
- `POST /api/devices/:id/mode` — set a WLED zone's mode: `manual` or `auto`
- `GET/PUT /api/settings/thresholds` — alert thresholds (min/max per metric + plug power draw); a reading outside its band flags that widget on the dashboard
- `GET /api/scenes` — house modes and their per-device target states
- `POST /api/scenes/:name/activate` — activate a scene; body may carry `wake_time` ("HH:MM") when activating Sleeping
- `GET /api/scenes/active` — current scene + activation time + pending wake time (if set)
- `GET /api/scenes/last-summary` — most recent Sleeping→Day overnight summary (null before the first); carries a `planner` section (today's events + overdue/high-priority tasks)
- `GET /api/events?from=YYYY-MM-DD&range=7d` — calendar events in a date window, recurring ones expanded into occurrences
- `POST /api/events`, `PUT /api/events/:id`, `DELETE /api/events/:id` — event CRUD (PUT is partial)
- `POST /api/health/ingest` — Health Auto Export JSON push → raw health tables (RR
  intervals, sleep stages, sleep HR, nightly resp-rate/SpO2/wrist-temp); rows keyed to a
  noon-to-noon "night" (see `app/health.py`; built pass-by-pass per `docs/health-build-plan.md`)
- `GET /api/health/latest-night` — latest night's raw values (the Health view's
  ingest-confirmation readout)
- `GET /api/health/night?night=YYYY-MM-DD` — consolidated derived readout for one night
  (default latest): per-night metrics (cleaned RMSSD/lnRMSSD, resting HR, nightly vitals),
  rolling baselines, and the recovery score with its per-metric breakdown
- `POST /api/health/recompute` — rebuild derived metrics/baselines/scores from stored raw
  (optional `?night=`; omit to rebuild all, e.g. after changing `HEALTH_*` weights)
- `GET /api/health/history?range=30d` — per-night recovery + sleep scores plus driver
  metrics (nightly vitals, stage minutes, onset/wake timestamps, debt/target) for the
  trend charts and the dashboard's sleep-detail and vitals-history dialogs
- `GET/PUT /api/health/settings` — user-owned personal sleep need (minutes); PUT recomputes
  sleep scores
- `POST /api/health/subjective` — log the morning's subjective 1–5 recovery feel
- `GET /api/health/correlation` — Pearson r of the subjective rating vs computed scores
  (the signal for hand-tuning `HEALTH_*` weights; the hub never auto-fits them)
- `GET /api/tasks?list=home&done=false` — filterable to-do list
- `POST /api/tasks`, `PUT /api/tasks/:id`, `DELETE /api/tasks/:id` — task CRUD (PUT is partial, including `done`)
- `POST /api/tasks/:id/complete` — one-tap task completion (idempotent)
- `POST /api/arduino/command` — send a raw `KEY:VALUE` protocol line to the Arduino (exists for any future wired actuator and manual testing; WLED zones do not use this)

## Frontend

- Dark theme: a PCB soldermask/silkscreen look (deep green-black surfaces, copper accent,
  mono silkscreen labels, a live "RX" serial ticker replaying raw `KEY:VALUE` lines) — this
  visual language is approved, keep it when touching the dashboard. Consult the
  `frontend-design` skill for direction on any new surface.
- **Three views behind a header VIEW switch (Board / Planner / Health), and only these
  three.** The **Board** stays a single scrolling page of widgets grouped into `.zone`
  sections: **Room conditions** (temp/humidity/light/CO2), **Power** (one plug-pair per
  WiFi plug: switch + power widget), **Lighting** (one card per WLED zone), **Motion**
  (PIR status + activity log). Any further **device type** must follow the same
  pattern: another `.zone` of widgets/cards on the Board, never a new view/tab. The
  **Planner** view (Calendar agenda + To-do panels, quick-add/edit forms, deep-linked
  as `/#planner`) and the **Health** view (Apple Health sleep/recovery data, deep-linked
  as `/#health`, built pass-by-pass per `docs/health-build-plan.md`) each earned a
  separate view only because neither is a device lane at all — don't take them as
  precedent for splitting device zones into tabs.
- Each Lighting card's top-right switch is always the zone's physical on/off, and it
  stays clickable in both modes — a user can switch a zone off even while it's in
  `auto` (the lighting job may reassert brightness/on on its next tick, but a manual
  on/off click is never blocked by mode). Mode (`manual`/`auto`) is one of the control
  rows alongside brightness/color/effect. In `auto` mode only the brightness control
  goes read-only (the lighting job drives it from lux) but keeps displaying the live
  value every poll rather than freezing or disappearing; color/effect stay editable in
  either mode since the auto job never
  touches them.
- Each widget expands on click into an **overlay dialog** (detail view: range-scoped chart,
  min/max/avg stats, "typical now" 7d-avg-by-time-of-day) rather than expanding in place —
  in-place expansion was rejected because it reflowed the grid out from under the cursor.
  Never make widget interaction shift the board layout.
- An alert-thresholds settings dialog (gear icon) lets the min/max band per metric (and
  plug power draw) be edited; a reading outside its band flags that widget on the board.
- A persistent MODE (scene) switch lives in the header — Sleeping/Day/Away, active one
  lit, pending wake ("→ Day 07:00") shown beside it — since a scene cuts across every
  device zone on the page. Sleeping opens a small dialog with the optional wake time,
  labeled as an auto-switch to Day, *not* an alarm; Day/Away activate on click. While a
  non-Day scene is active, auto-mode lighting cards read "Auto paused — ‹scene› scene
  active."
- The overnight summary is a dismissible card at the top of Room conditions, visible
  while the house is in Day and there's an undismissed Sleeping→Day summary (dismissal
  is remembered per-summary in localStorage). Its planner half ("Today" / "Needs
  attention") renders from the summary's `planner` key and hides when absent.
- Planner UI conventions: the calendar is an Apple-Calendar-style **time grid** with
  Day/Week/Month views (weeks start Monday, compact 24h times, copper now-line on
  today) — timed events occupy their duration as blocks tinted by their category color,
  overlapping events share the column side by side. A drag on the day/week grid can
  start in one day column and end in another → a **multi-day timed** event, clipped
  into each day column it spans; a drag across **month** cells (and a plain month click,
  since month is day-granular) makes an **all-day** event. All-day events render as
  spanning bars in the all-day row above the day/week grid (lane-packed, `‹ ›` arrows
  when they run past the visible edge) and as continuation chips across month cells; the
  dialog has an All-day toggle that swaps the start/end inputs between datetime and date
  (its "Ends" date is the inclusive last day; the API stores the exclusive day-after).
  Creating an event never uses an always-visible form: the + button, a grid drag/click,
  or a month cell all open the event dialog; clicking an existing event/bar/chip opens
  the same dialog prefilled as its detail/edit view (with Delete). Recurring events show
  `↻` and edit/delete as a whole series. The to-do list is deliberately flat and slim:
  rows with due-date/priority chips, one-tap square-pad checkbox, a small + toggling the
  inline add row (which ✎ reuses for edits), done tasks behind "Show done". The tasks
  `list` column is surfaced as **two separate stacked to-do widgets** in the right
  column beside the calendar, under one shared "To-do" zone caption — **Life stuff**
  (`list = "life"`) above **University** (`list = "university"`), each its own card with
  its own count, + button, add/edit row and Show-done toggle. They're generated by
  cloning `#task-card-tpl` once per entry in `TASK_LISTS` (app.js), each wired as a
  self-contained `TaskWidget` with its own `editingId`/`showDone`; all share the one
  `plannerTasks` fetch. "University" is an exact match and "Life stuff" is the catch-all
  (untagged/legacy-tagged tasks live there so nothing is ever orphaned). New tasks get
  the widget's list; editing a task never moves it between lists. Planner data loads on view
  switch and refreshes on the 30s tick (skipped mid-drag or while the event dialog is
  open) — never part of the 5s device poll.
- Keep it a single lightweight web app served by the host — no separate build
  infrastructure beyond what's needed for a small SPA or server-rendered pages.
- Live-ish updates (short polling interval or simple WebSocket) rather than manual
  refresh, but nothing resource-heavy — this is still constrained by the 8GB host.

## Conventions

- Keep firmware (`/firmware`) and host (`/server`, `/dashboard` or similar) cleanly
  separated in the repo.
- Comment the serial protocol wherever it's touched on either side — it's the seam most
  likely to drift out of sync between firmware and host if changed carelessly.
- Favor explicit, readable code over cleverness — this runs unattended 24/7; failures
  should be loud in logs, not silent.
- Don't add cloud dependencies, telemetry, or external services beyond what's already
  decided here (Tailscale, myStrom local API) without flagging it first.
