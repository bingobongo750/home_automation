# CLAUDE.md — Smart Home Hub

This file gives Claude Code persistent context for this repository. Read it before making
architectural decisions. It reflects real hardware/software choices already made — don't
relitigate them without a clear reason.

## Project summary

A DIY smart home hub. A Raspberry Pi 4 (4GB RAM) runs 24/7 as the central server, sensor
database, and web dashboard, booting from a 32GB microSD card. An old MacBook (8GB RAM) that
previously held this role stays around as a fallback/dev machine — it can run the full
stack under `MOCK_HARDWARE=1` for development, or take over host duty if the Pi is down,
but the Pi is the source of truth whenever it's up. An Arduino Due handles all wired,
time-sensitive I/O over a single USB serial connection to the host. WiFi devices —
currently two myStrom smart plugs and two Shelly smart bulbs (ambient-lighting zones) —
are controlled directly by the host over the network — no cloud, no third-party hub, no
Home Assistant.

**Hard constraint:** the host is a 4GB RAM Raspberry Pi 4. Never introduce video
transcoding, AI/camera vision, Docker-heavy stacks, or anything with a large idle memory
footprint. Everything on the host should be lightweight I/O: read serial, write SQLite,
serve HTTP, call REST APIs.

## Two device lanes — do not blur these

1. **WIRED lane** (Arduino Due ↔ host via USB serial)
   All sensors and any future relay/MOSFET actuators wire into the Arduino via
   breadboard — never directly into the host. Time-sensitive timing logic lives
   entirely on the Arduino. The host only ever sends short, high-level text commands
   over serial and reads short structured lines back.

2. **WIRELESS lane** (host ↔ WiFi devices directly, bypassing the Arduino entirely)
   The host's server calls each WiFi device's local REST API directly over the LAN.
   Currently two device types:
   - Two **myStrom WiFi Switch** plugs (Swiss Type J, local REST API, power monitoring
     2–3680W), named for what they run: **"Coffee machine"** (`192.168.0.51`) is
     physically installed, **"Desk"** (`192.168.0.52`) is seeded in the `devices`
     table as a placeholder IP until it's physically installed. (They were "Plug 1"
     and "Plug 2"; `init_db()` renames those in place before the seed loop, so an
     existing row is renamed rather than duplicated.)
   - Two **Shelly Multicolor Bulb E27 Gen3** smart bulbs ("Cupboard", the bulb in
     the cupboard, and "Room LED", the room's ambient bulb) — self-contained WiFi
     RGBW bulbs (local JSON-RPC API over HTTP, no cloud required) that screw into
     an ordinary E27 fixture; the fixture only ever supplies mains power, all
     control electronics live in the bulb itself. **Both are provisioned and
     reachable as of 4 Aug 2026** (`192.168.0.61` / `.62`, both answering
     JSON-RPC) — this section previously said neither was installed. "Cupboard"
     is confirmed to physically light the room the BH1750 sees: dimming it
     134 → 94/255 moved the measured level 8 → 6 lx, which is what the
     auto-lighting loop closes on.
   No cloud account or app is required for runtime control, only initial WiFi
   provisioning. Devices are modeled generically (see below) so further WiFi plugs or
   lighting zones can register without rewriting existing device logic.
   The hub covers a **single room**, so the `devices.room` column is seeded empty
   for every device and the dashboard shows the device name alone — the column
   stays in the schema and API for a future multi-room hub, but don't reintroduce
   per-device room labels here.

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

## Smart bulb lighting integration

- Each ambient-lighting zone is a single **Shelly Multicolor Bulb E27 Gen3** — a
  self-contained WiFi RGBW smart bulb (local JSON-RPC API over HTTP, no cloud
  required) screwed into an ordinary E27 fixture. Same device abstraction as the
  myStrom plug, `type = 'bulb_zone'`, with an added `mode` column: `manual` or
  `auto`. There is no separate microcontroller or addressable strip per zone — the
  bulb *is* the whole device; the fixture it screws into only ever supplies mains
  power.
- `app/shelly_bulb.py` is the client (get state; push a partial on/brightness/color
  update) — mirrors `app/mystrom.py`'s shape, including a mock for `MOCK_HARDWARE=1`.
- **Auto lighting is a closed loop on a setpoint, not a curve.** `app/lighting.py` is a
  **separate** background thread (not the plug poller, not the serial reader) that, on
  its own interval (`LIGHTING_POLL_INTERVAL`), drives the *measured* BH1750 level to
  `target_lux` — **user-owned**, stored in `settings`, edited from the dashboard's
  settings dialog via `GET/PUT /api/settings/lighting`, seeded from
  `LIGHTING_TARGET_LUX` (default **5 lx**). Zones in `manual` mode are left alone; the
  dashboard drives those via `POST /api/devices/:id/state`.
  - **Auto owns brightness; the user owns on/off.** The loop sends `brightness` and
    **never `on`**, and each tick it reads every auto zone's switch and drives only the
    ones that are ON. Two consequences: a switched-on zone bottoms out at the Shelly's
    1 % floor rather than going dark (0 is not sendable — "off" only exists through
    `on`), which is nearly always invisible since the loop only wants zero light when
    ambient already beats the target; and when **no** auto zone is on the loop **holds
    its integrator** rather than integrating against a room it cannot affect, or
    flipping a switch back on would blast whatever it had wound up to. `target_lux` of
    0 likewise means *hands off entirely*, not "dim everything to 1 %".
    `GET /api/devices` reports `auto.state = "zones_off"` on a switched-off zone
    instead of the shared controller state, which would otherwise claim it was
    converging.
  - It **replaced an open-loop linear ramp** (full brightness at pitch dark fading to
    off at a `lux_off` cutoff). A ramp cannot hold a level because it never aims at
    one, and the mapping is circular: the BH1750 measures TOTAL illuminance, including
    the light our own bulbs emit, so "the room got brighter" and "our bulbs are
    working" are the same signal. A stored `lux_off` is **ignored, not converted** —
    one was the top of a fade, the other is a target. `lux_off` is also redundant now:
    a zone switches itself off whenever ambient alone already exceeds the target.
  - The control law is **pure and lives in `app/lighting_control.py`** (no DB/Flask/
    clock, so it unit-tests against a simulated room). It is **integral-only**:
    the brightness→lux gain is unknown and unmeasured (bulb, room reflectance, sensor
    placement), and integral control converges to zero steady-state error without
    knowing it. No derivative — lux arrives as **integers** from the firmware, so at a
    5 lx setpoint ±1 lx is already ±20 %, and differentiating that is pure noise. Gain
    is **hand-tuned via `LIGHTING_GAIN`, never auto-fitted**: ambient drifts on its own,
    so any observed Δlux mixes "our bulb did that" with "the room changed", and a gain
    estimator fed that chases its tail. Same stance as the `HEALTH_*` weights.
  - Three things keep it stable, and **the third is not optional** — it was found by
    the convergence tests, not by reasoning: a **deadband**
    (`LIGHTING_DEADBAND_LUX`, below ~1.0 it hunts on quantisation alone), a **slew
    limit** (`LIGHTING_MAX_STEP`), and **overshoot-reactive step scaling**. A single
    fixed gain cannot serve a plant gain spanning decades: with a strong lamp near the
    sensor the old fixed cap was coarser than the whole deadband and produced a perfect
    limit cycle (0 → 16 units → 16 lx → 0 → 0 lx → forever). So every time the error
    changes sign the slew cap halves; while the sign holds it relaxes back (×1.25).
    That state (`step_scale`, `sign`) is carried between ticks — dropping it
    reintroduces the oscillation, which is why `correct()` takes and returns it.
  - **Never correct twice on the same measurement.** After a brightness *change* the
    next reading must be `LIGHTING_SETTLE_S` newer (the Arduino only reports every 5 s)
    and must be a sample not already acted on. Without this the loop integrates one
    error repeatedly and overshoots every time. The settle clock restarts on a
    **change**, not on every push — the loop re-asserts its current value each tick so
    a zone knocked out of sync by a manual tap comes back, and counting those as
    changes would keep the guard permanently armed and the loop permanently blind.
    "No new sample yet" is routine and must **not** surface as a fault; only a reading
    older than `LIGHTING_STALE_AFTER_S` reports `stale`, and between samples the card
    keeps showing the last real verdict.
  - **One controller for all zones, not one per zone.** One BH1750, one room. Two
    independent controllers on the same sensor each see the other's light, each
    conclude they are short of target, and both keep pushing — the combined output
    overshoots roughly two-fold, then they fight on the way back down. If this hub ever
    covers more than one room, a controller per sensor is the first thing to change.
  - **Saturation is a reported state, not a failure.** "The room is already brighter
    than the target" is the normal daytime outcome. `lighting.status` (`state`,
    `detail`, `target_lux`, `measured_lux`, `brightness`) rides on every `auto` bulb row
    in `GET /api/devices`, and the card prints it in copper — otherwise auto mode
    looks broken when it is working correctly. States: `holding`, `converging`,
    `too_bright`, `at_max`, `off`, `no_reading`, `stale`.
  - **MOCK_HARDWARE cannot show convergence**: the fake lux generator is independent of
    the fake bulbs (coupling them would wire the wired lane to the wireless one), so
    auto mode there only ever reaches `too_bright`/`at_max`. Convergence lives in
    `tests/test_lighting_control.py`, which simulates the room instead.
- **Ambient lighting is CCT, not RGB.** The bulb has a dedicated white channel and
  separate R/G/B dies, and lights only one at a time (`mode`). The ambient/warmth
  control sends `ct` in kelvin and the bulb runs its white channel; `rgb` is reserved
  for the dashboard's "Custom" colour picker. This is a brightness decision, measured
  on the real bulb at full brightness: `cct` 2700 K draws **8.7 W**, the equivalent
  warm white mixed from the RGB dies draws **5.5 W**, and the lumen gap is wider still
  because RGB dies are far less efficacious per watt than a white phosphor. Faking
  warm white out of the colour dies is what made ambient lighting dim — never route
  the warmth control back through `rgb`.
- `ct` limits are **2700–6500 K and are hard**: the bulb answers anything outside them
  with JSON-RPC error `-103` rather than clamping, so a caller must clamp first
  (`shelly_bulb.clamp_ct`). This is why the warmth slider starts at 2700 K and not the
  1800 K "candle flame" it used to — that floor was only reachable while ambient was
  faked from RGB. Sub-2700 K amber is still available, dimly, via Custom colour.
- `color` and `ct` are **mutually exclusive** in `POST /api/devices/:id/state`, in
  `shelly_bulb.set_state()` and in a scene's target — sending both would make the
  winner depend on `mode`. A brightness-only update touches neither, so the
  auto-lighting job never knocks a zone out of ambient white.
- The **ambient colour ramp** (`WARMTH_RAMP` in `dashboard/app.js`) is now **preview
  only** — it tints the swatch and the slider track and never reaches the hardware.
  It is deliberately not the black-body/Planckian values every Kelvin-to-RGB tool
  returns: those are calibrated for a screen's D65 white point and read colder as a
  small UI swatch than the light the bulb actually emits. The CSS track reads the same
  ramp through the `--warmth-gradient` custom property.
- Physical setup (screwing in the bulb, WiFi provisioning, static IP) happens later —
  the backend, database schema, and API already assume both zones exist from day one,
  same as the myStrom plug.

(An earlier plan wired an addressable WS2812B/NeoPixel strip directly to the Due for a
CO2 traffic-light indicator, and drove the two ambient zones off WLED-flashed ESP8266
boards + strips. Both were dropped in favor of the Shelly bulbs above — simpler, no
soldering/wiring/power-injection, and the wired indicator wasn't essential. Treat any
future wired addressable lighting as a fresh addition, not a revival of that plan.)

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
  - **Sleeping** — every bulb zone off, every unlocked plug off.
  - **Home** — every unlocked plug on; deliberately *no* zone targets (see
    suppression below).
  - **Away** — every bulb zone off, every unlocked plug off.
  (Sleeping and Away share device targets; they differ in the wake-time
  scheduling and morning summary that only Sleeping carries.)
- The active scene (name + activation timestamp + pending wake time) persists in
  the `settings` table so it survives backend restarts. Never-activated counts
  as "Home" — normal operation.
- **Nightly schedule:** a stored sleep window (`GET/PUT /api/settings/sleep-schedule`,
  default 00:00 → 09:30, edited in the settings dialog) activates Sleeping every night
  and hands back to Home in the morning, reusing the wake machinery below so the morning
  summary works exactly as it does manually. Its bedtime timer is deliberately
  **separate** from the wake timer: a manual scene change must beat tonight's pending
  wake without cancelling tomorrow's bedtime. A restart mid-window does not back-fill —
  it arms the next bedtime and leaves the current scene alone, so coming back up at
  02:00 never overrides a house someone deliberately put in Away. Scene changes only,
  never an alarm.
- **Auto-lighting suppression:** while any scene other than "Home" is active, the
  lux-based auto job in `app/lighting.py` is paused wholesale — the scene's
  explicit values win. A scene never rewrites a zone's `mode` column, so
  returning to "Home" resumes lux control on any zone still set to `auto`
  (activation pokes the lighting job so it reacts immediately, not a tick later),
  and `manual` zones stay wherever the scene/user left them.
- Locked plugs are never switched by a scene — skipped and reported per-device in
  the activation response, same protection as the dashboard toggle. One
  unreachable device never blocks the rest of a scene.
- **Wake time (Sleeping → Home):** activating Sleeping accepts an optional
  `wake_time` ("HH:MM", local). A plain in-process `threading.Timer` (no
  job-queue dependency) then switches the scene to Home at that time. It ONLY
  switches the scene — explicitly not an alarm: no sound, no notification.
  Blank/absent means Sleeping holds until changed manually. Any scene activation
  cancels the pending timer (a generation counter makes a stale timer that
  already started firing a no-op). The pending wake is persisted with the active
  scene and re-armed at startup; one that came due while the backend was down
  fires immediately.
- **Morning summary:** every Sleeping → Home transition (scheduled or manual)
  computes overnight stats from the existing `readings` table over the Sleeping
  window — temp/hum min/max/avg, CO2 average plus start vs end (flagged if it
  climbed ≥ 200 ppm; the dashboard headlines the average, since a signed delta
  up front reads like a negative CO2 level), motion count + event times — and
  stores the result in `settings` for `GET /api/scenes/last-summary`. Computed
  once at the transition, no new report system or table.
- Scene behavior is covered by `server/tests/` (stdlib unittest, runs fully
  under `MOCK_HARDWARE=1`): `cd server && python3 -m unittest discover -s tests`.

## Presence (automatic Away)

- `app/presence.py`, built to `docs/presence-design.md`. Two iPhone Shortcuts POST
  departure/arrival; **the phone reports presence, the host decides the scene**, so the
  phone holds no copy of the sleep schedule and changing that schedule never means
  editing a Shortcut. Imports `scenes`, never the reverse.
- **Away is the strongest state.** Nothing automatic overrides it — `_fire_bedtime()`
  skips an Away house (otherwise the nightly timer puts an empty flat into Sleeping and
  the morning wake then switches it to Home, lights on in a house nobody is in), and
  activating Away already cancels a pending wake. Only an arrival or a manual pick ends it.
- **Departure is delayed, arrival is immediate.** A departure applies only after
  `PRESENCE_DEPART_GRACE_S`, so a bouncing geofence cannot strobe the room; a second
  departure does not restart the countdown. Arrival gets no grace — the point is lights
  on when you walk in — and **only ever ends Away**: a house in Home or Sleeping is left
  alone, which is what stops a stray geofence event overriding a scene chosen by hand.
- Arrival resolves to **Home or Sleeping** from the stored nightly window (wrap-past-
  midnight handled), carrying the schedule's own `wake_time` when it lands inside, so
  getting home at 02:00 still produces the morning summary.
- **The away summary is computed in `scenes.activate()`, not in `presence.arrived()`**,
  so it fires on *every* way out of Away — a phone arrival, or you tapping Home on
  the dashboard because the Shortcut never made it. That second case is the one that
  most needs it, and an earlier version silently produced nothing there.
- **The away summary's window ends `PRESENCE_ARRIVAL_TRIM_S` before the detected
  arrival.** You reach the door before the hub knows, and the PIR/CO2/lux/plug draw in
  that gap are all you — untrimmed, every homecoming reads as a break-in and the summary
  becomes noise. Absences under `PRESENCE_SUMMARY_MIN_S` produce no summary at all.
- **Repeated detections collapse into events** (`scenes.cluster_events`,
  `DISTURBANCE_COOLDOWN_S`). A PIR emits a sample per cycle while it sees movement, so
  counting raw rows would report "47 disturbances" for one person crossing the room.
- CO2 is a second, independent occupancy signal (a person sitting still defeats a PIR,
  not CO2) and is **omitted rather than zeroed** when the window holds no valid reading —
  a broken sensor must never render as "all clear".
- Covered by `server/tests/test_presence.py`.

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
- **Apple Calendar import** (`app/caldav_sync.py`): read-only, one way. Imported
  events are mirrors with `source = 'caldav'` — the API refuses to edit or delete
  them (409) and the dashboard renders them dashed and opens the dialog locked.
  Two-way sync is deliberately *not* built: deletions, conflicts and recurring
  exceptions are where CalDAV goes wrong, and this planner's recurrence model is
  `none|daily|weekly` rather than RFC 5545, so writing back would be lossy by
  construction. Apple's RRULEs are instead **flattened into concrete occurrences**
  across a rolling window, which is what lets a `FREQ=MONTHLY` come across at all.
  A sync replaces the `caldav` rows wholesale (the only thing that handles upstream
  deletions correctly) and skips the write entirely when a content hash is
  unchanged, so an idle calendar doesn't rewrite the table onto the SD card every
  15 minutes. Local events are never touched.
  **The protocol is hand-rolled over `requests`** — the `caldav` library pulls in
  lxml, an HTTP/3 stack and 16 packages for four XML requests, against a documented
  lightweight-I/O constraint. Only `icalendar` + `recurring-ical-events` were added
  (pure-Python), for parsing and RRULE expansion, which is the part not worth
  hand-rolling. Credentials are an Apple **app-specific password** in `.env` on the
  Pi only. Covered by `server/tests/test_caldav.py`.
- Both tables carry an `external_uid` column — the upstream UID on imported rows,
  and reserved on `tasks` for the same purpose; keep any new planner fields plain.
- **Morning summary integration:** the Sleeping→Home summary embeds
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
  on 4GB RAM — preferred over Node/Express for this project unless you have a strong
  reason to deviate).
- Serial: `pyserial` for Arduino communication over USB.
- Database: SQLite for sensor time-series data and device state history.
- Remote access: Tailscale (assume it's available; don't build custom auth/tunneling).
- Storage: the Pi boots from and runs entirely off a 32GB high-endurance microSD card —
  OS, the sensor DB and its backups all live on it. Nothing binds the hub to a storage
  medium (`DB_PATH` is an env var, the server has nothing machine-specific), so this is
  revisitable without code changes. Two consequences to keep in mind: nothing in the
  codebase ever prunes `readings`/`power_readings` — no retention, no downsampling, no
  `VACUUM`, so the DB grows ~3GB/year, linearly and forever — and the card is the most
  likely component on the box to fail, so the nightly `.backup` is copied off the Pi
  rather than kept only on the card. See `MANUAL.md` §7.1 and §7.9.
  (An earlier plan booted the Pi from a 1TB USB-C SSD that would also have served Time
  Machine and file shares over Samba. Dropped — the SSD stays direct-attached to the
  Mac. If a NAS role ever comes back it's a new decision, not a revival of that one.)
- Fallback dev machine: the old MacBook can run the full stack (under `MOCK_HARDWARE=1`
  for hardware-free development, or for real against the Arduino/WiFi devices if it's
  ever swapped in as host). Don't write anything into the server that assumes it's
  running on Pi-specific hardware — it should run unmodified on either machine.
- No MQTT broker yet — single Arduino node for now. Keep the device/data layer decoupled
  enough that adding Mosquitto later for multi-room nodes doesn't require a rewrite.
- Sensor readings are checked against a physically plausible band
  (`METRIC_RANGE` in `app/serial_reader.py`) and an out-of-band value is logged
  loudly — but **stored anyway**. Ingest never drops a reading it dislikes: a
  failing sensor emits in-protocol nonsense rather than going quiet (the SCD4x
  reports `CO2:0` for an invalid channel), and filtering that made a broken
  sensor look merely idle, which is unreviewable from the dashboard. Raw
  telemetry stays raw. If implausible rows need excluding from averages, do it
  at the query layer where it can be switched off — not at ingest.
- `MOCK_HARDWARE=1` env var (see `.env.example`) swaps the serial reader, the myStrom
  client, and the Shelly bulb client for fake data generators (plausible sinusoidal
  sensor drift, wobbling plug wattage, in-memory zone state) so the dashboard is
  developable end-to-end without any hardware attached. Keep this mode working when
  touching `serial_reader.py`, `mystrom.py`, or `shelly_bulb.py`.

## API shape (backend)

Implemented in `server/app/api.py` (planner endpoints in `server/app/planner.py`) —
keep this list in sync when endpoints change:

- `GET /api/sensors/latest` — most recent reading per sensor
- `GET /api/sensors/history?metric=temp&range=24h` — time series for charts (range: `30m`/`24h`/`7d` style)
- `GET /api/sensors/stats?metric=temp` — 24h min/max/avg + 7d avg, for a widget's expanded view
- `GET /api/sensors/profile?metric=temp&bucket=30` — "typical day" curve: 7-day average per time-of-day bucket
- `GET /api/motion/events?range=24h` — recent motion detections + count, for the activity log
- `GET /api/devices` — list known devices (two myStrom plugs, two Shelly bulb zones); plug rows carry `power` (last polled sample), bulb rows carry `mode` and `light` (live state)
- `GET /api/devices/:id` — device row with the same per-type fields as above
- `POST /api/devices/:id/toggle` — turn a WiFi plug on/off
- `GET /api/devices/:id/power/stats` — 24h/7d average draw + estimated 24h kWh
- `GET /api/devices/:id/power/history` — power draw over time
- `POST /api/devices/:id/state` — set a bulb zone's on/brightness/`ct`/color (any subset);
  brightness is 0-255 hub-wide, converted to the Shelly's 1-100% only inside `app/shelly_bulb.py`.
  `ct` (2700-6500 K) drives the white channel — the ambient control, and much brighter;
  `color` drives the RGB dies. The two are mutually exclusive; responses carry `color_mode`
- `POST /api/devices/:id/mode` — set a bulb zone's mode: `manual` or `auto`
- `GET/PUT /api/settings/thresholds` — alert thresholds (min/max per metric + plug power draw); a reading outside its band flags that widget on the dashboard
- `GET/PUT /api/settings/lighting` — auto-lighting `target_lux`: the measured room level an `auto` zone holds (closed loop; 0 disables auto lighting). Saturation is reported per-zone, not hidden
- `GET/PUT /api/settings/sleep-schedule` — nightly Sleeping window (`enabled`, `sleep_time`, `wake_time` as local "HH:MM"); PUT re-arms it immediately
- `GET /api/scenes` — house modes and their per-device target states
- `POST /api/scenes/:name/activate` — activate a scene; body may carry `wake_time` ("HH:MM") when activating Sleeping
- `GET /api/scenes/active` — current scene + activation time + pending wake time (if set)
- `GET /api/scenes/last-summary` — most recent Sleeping→Home overnight summary (null before the first); carries a `planner` section (today's events + overdue/high-priority tasks)
- `GET /api/scenes/last-away-summary` — most recent Away→(Home|Sleeping) disturbance summary (null before the first)
- `GET /api/nights?range=30d` — every recorded Sleeping window, newest first, each with its stored summary plus an `anomalies` list (metrics >2 SD from the window's own mean; awakenings use a plain threshold since a high count means something on its own)
- `GET /api/nights/:date` — one night's full summary, plus `awakenings` (`start`/`end`/`samples`/`duration_s` per time you got up, re-derived from `readings` so nights recorded before it existed get it too); `DELETE` removes it (a stray Sleeping toggle records a junk few-minute "night" that would otherwise drag the mean the anomaly flags are measured against)
- `GET /api/nights/:date/series?metric=temp` — that metric's curve across the night's stored window (`temp|hum|co2|lux`), for the dialog's expandable stats
- `GET /api/presence` — presence state + whether a departure is waiting out its grace
- `POST /api/presence/departed` / `POST /api/presence/arrived` — phone-driven presence (no body, so an iPhone Shortcut is one action); see `app/presence.py` and `docs/presence-design.md`
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
- `POST /api/arduino/command` — send a raw `KEY:VALUE` protocol line to the Arduino (exists for any future wired actuator and manual testing; bulb zones do not use this)
- `GET/POST /api/arduino/serial` — release the serial port for reflashing (`{"paused": true, "minutes": 20}`) without stopping the hub; the pause always self-expires (default 15min, capped 2h)

## Frontend

- Dark theme: a PCB soldermask/silkscreen look (deep green-black surfaces, copper accent,
  mono silkscreen labels, a live "RX" serial ticker replaying raw `KEY:VALUE` lines) — this
  visual language is approved, keep it when touching the dashboard. Consult the
  `frontend-design` skill for direction on any new surface.
- **Three views behind a header VIEW switch (Board / Planner / Health), and only these
  three.** The **Board** stays a single scrolling page of widgets grouped into `.zone`
  sections: **Room conditions** (temp/humidity/light/CO2), **Power** (one plug-pair per
  WiFi plug: switch + power widget), **Lighting** (one card per bulb zone), **Motion**
  (PIR status + activity log). Any further **device type** must follow the same
  pattern: another `.zone` of widgets/cards on the Board, never a new view/tab. The
  **Planner** view (Calendar agenda + To-do panels, quick-add/edit forms, deep-linked
  as `/#planner`) and the **Health** view (Apple Health sleep/recovery data, deep-linked
  as `/#health`, built pass-by-pass per `docs/health-build-plan.md`) each earned a
  separate view only because neither is a device lane at all — don't take them as
  precedent for splitting device zones into tabs.
- Each Lighting card's top-right switch is always the zone's physical on/off, and it
  stays clickable in both modes. **It is a master gate and it beats the auto loop:**
  the user decides which lamps may light at all, and a zone switched off stays off no
  matter what auto mode wants. It used to be the opposite — the job pushed `on=True`
  every tick, so switching a zone off while in `auto` undid itself seconds later. The
  card says so per zone ("Switched off — auto lighting will not turn it on."), because
  the backend's `auto` status describes the zones actually being driven. Mode (`manual`/`auto`) is one of the control
  rows alongside brightness/color. In `auto` mode only the brightness control
  goes read-only (the lighting job drives it from lux) but keeps displaying the live
  value every poll rather than freezing or disappearing; color stays editable in
  either mode since the auto job never touches it. There is **no effect control** —
  the Shelly bulb has no effect engine (numbered effects were a WLED feature, dropped
  with the rest of that plan).
- **Drag across any expanded chart** to select a time span and get its statistics
  (average headlined, plus min/max, the span, its duration and the sample count) in a
  readout under the chart. **Power charts additionally show energy** (kWh, or Wh below
  10 Wh where kWh would render as 0.00) — watts are a rate, so a span of them integrates
  into the number you actually want off a power chart. Trapezoidal over the samples in
  the selection, *not* avg × duration: the series is bucket-averaged and can have gaps
  where the plug was unreachable, and avg × duration silently bills those gaps at the
  average rate. The crosshair already answers "what was it at this moment";
  this answers "what was it across this stretch", which otherwise means eyeballing a
  line. Selection is horizontal only — the question is always about a time range, never
  a value range — and clears on a plain click, a range/metric change, or closing the
  overlay, since a selection belongs to the chart that produced it.
- Every detail chart opens on **3h** (`DEFAULT_DETAIL_RANGE`), **every time** — the range
  resets on open rather than persisting. A 7d frame picked while reading CO2 must not
  silently become how the next metric is read; the axis label is easy to miss, and
  misreading a week as a day is worse than one extra click.
- Each widget expands on click into an **overlay dialog** (detail view: range-scoped chart,
  min/max/avg stats, "typical now" 7d-avg-by-time-of-day) rather than expanding in place —
  in-place expansion was rejected because it reflowed the grid out from under the cursor.
  Never make widget interaction shift the board layout.
- A settings dialog (gear icon) with three sections: **Alert thresholds** (min/max band
  per metric and plug power draw — a reading outside its band flags that widget on the
  board), **Auto lighting** (the `target_lux` setpoint), and **Nightly sleep** (the recurring
  Sleeping window). Each section posts to its own endpoint on save.
- Chart y-axes never run negative except temperature — humidity, lux, CO2 and watts
  have no negative values, so `METRICS.allowNegative` gates the axis padding and
  everything else clamps at 0.
- A persistent MODE (scene) switch lives in the header — Sleeping/Home/Away, active one
  lit, pending wake ("→ Home 07:00") shown beside it — since a scene cuts across every
  device zone on the page. Sleeping opens a small dialog with the optional wake time,
  labeled as an auto-switch to Home, *not* an alarm; Home/Away activate on click. While a
  non-Home scene is active, auto-mode lighting cards read "Auto paused — ‹scene› scene
  active."
- **Two summary cards**, both at the top of Room conditions, both shown in **any** scene,
  each staying until dismissed or replaced by a newer one — come home late and go straight
  to bed and next morning you want both: the away card covering the evening out, the
  overnight card covering the night after. Gating either on the active scene was a bug.
  The **away card sits above** the overnight one (it is the one that might need acting on)
  and leads with a **disturbance timeline** — the absence as a track with a tick per
  movement event, since *where* in the absence something happened is the first thing you
  want and a list of timestamps cannot show it. Room conditions on that card are one quiet
  line, not stat tiles: giving temperature equal visual weight would bury the
  disturbances. Dismissal is remembered per-summary in localStorage. The overnight card's
  planner half ("Today" / "Needs attention") renders from the summary's `planner` key and
  hides when absent.
- **"I'm home"** appears in the header MODE switch **only while Away**. It posts to
  `/api/presence/arrived`, *not* a scene, so the host picks Home or Sleeping from the
  nightly window — tapping it at 02:00 puts the house to bed rather than switching every
  light on. It is the manual fallback for an arrival Shortcut that did not fire.
- **Nights widget** (Motion zone, since a night is bounded by the scene rather than by a
  sensor): every recorded Sleeping window. Expands to a 7/30/60d trend over
  temp/humidity/CO2/awakenings, a per-night list, and a per-night dialog. Anomalous values
  render red, and a night can be **deleted** from that dialog — a stray Sleeping toggle
  records a junk few-minute "night" that would otherwise drag the mean those flags are
  measured against.
- **The per-night dialog answers "when did I get up", not just how often.** It leads with
  a **timeline** — the sleep window as a track, one mark per awakening — reusing the away
  card's `.away-track`/`.away-axis` language, for the same reason: *where* in the night
  something happened is the first thing you want, and a list of timestamps cannot show it.
  Each mark's **width is its duration** (with a 3px floor so a brief stir stays visible),
  so a 20-minute trip at 3am reads differently from a momentary one. The times are also
  listed as text underneath — ticks can overlap on a phone, and the list carries the
  durations.
  Below it, **each stat tile expands into that metric's actual curve across the night**
  (`GET /api/nights/:date/series`), inline in the same dialog rather than stacking a
  second overlay: click Temp avg and you get the temperature over those 9.5 h, with the
  usual drag-to-select statistics. Clicking the open tile again collapses it.
  **"Got up" is deliberately not expandable** — motion is discrete events, and a line
  drawn through them would invent a curve that does not exist; the timeline *is* its
  chart. That is also why `/series` rejects `metric=motion` with a 400.
  `awakenings` is re-derived from `readings` on read rather than widened into the stored
  summary: raw rows are never pruned, so every night already in the history gains
  durations without a migration.
- **Calendar sizing: one user-owned knob, one fixed constant.** These are two
  different questions and were wrong when conflated, so keep them distinct even
  though only one is adjustable now:
  - **`--cal-size`** is the *physical size of the calendar window on the page*: the
    grid box's visible height (`720px ×`), the month row height (`86px ×`), and the
    page width the planner claims. A plain multiplier (0.7–1.6, 0.1 steps) set by
    `app.js` from a **size** stepper in the toolbar, with a reset; persisted in
    localStorage. Every dimension is derived from it in CSS. Above 1.0
    `#view-planner` breaks out of `main`'s 1180 px column, staying centred and
    capped at `100vw - 40px` so it can never overflow the screen; at exactly 1.0 it
    resolves to `main`'s content box, so the default layout is untouched.
  - **`--cal-hour-px`** is the *scale inside* that window — an hour's height,
    **fixed at 28 px** and declared on `:root` in `styles.css`. It had a **scale**
    stepper (default 56, range 28–160); the user settled on 28 — which that stepper
    displayed as "50%" — and asked for the control to be removed. It is still the
    single definition both files share: CSS declares it and `app.js` reads the
    computed value back via `calHourPx()` rather than hardcoding 28 again. Don't
    "simplify" that into a JS constant — the height used to be `44` in JS *and*
    three places in CSS behind a "keep in sync" comment, which is not a mechanism.
  Window height is **never read back out of the layout.** It used to be: a CSS
  `resize: vertical` grip on `.cal-scroll` was captured with a `ResizeObserver` and
  persisted, but `box-sizing` is border-box and `.cal-scroll` has a 1px top border, so
  `contentRect.height` came back one pixel short of the `max-height` that produced it
  — saved, applied, observed shorter again. The calendar walked itself smaller a pixel
  per render and localStorage carried the damage across reloads. Don't reintroduce a
  measure-and-store loop for a dimension the user sets.
- Planner UI conventions: the calendar is an Apple-Calendar-style **time grid** with
  Day/Week/Month views (weeks start Monday, compact 24h times, copper now-line on
  today) — timed events occupy their duration as blocks tinted by their category color,
  overlapping events share the column side by side. A drag on the day/week grid can
  start in one day column and end in another → a **multi-day timed** event, clipped
  into each day column it spans; a drag across **month** cells (and a plain month click,
  since month is day-granular) makes an **all-day** event.
  **Creating from the grid is mouse-only** (`pointerType === "mouse"` in
  `onGridPointerDown` / `onMonthPointerDown`) — on a touch screen the grid is inert.
  It briefly had a tap-to-create for touch and that was a mistake: the grid is a tall
  scroll surface, so a finger lands on it constantly just getting down the page, and
  every contact opened the new-event dialog. No gesture separates "create here" from
  "I was scrolling", because the discriminator on a desktop is the drag and on touch
  the drag *is* the scroll — so tap-to-create fires on the accidents and still can't
  express a duration. On touch, the **+** button is the only way to create; tapping an
  existing event/bar/chip still opens it (those are `click` handlers, specific enough
  not to be hit by accident). Don't reintroduce tap-to-create. The `.cal-hint` text
  and `.cal-col`'s crosshair cursor swap on `(pointer: coarse)` to match — keyed on
  input type, not width, since a mouse drag works at any window size.
  All-day events render as
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
- **Phone layout: one DOM, one stylesheet, media queries.** The phone gets a
  different *layout*, never a different page — a separate UA-sniffed mobile
  document was considered and rejected (two dashboards to keep in step, and
  whichever one you aren't looking at is the one that rots). Everything lives in
  a single `@media (max-width: 700px)` block at the end of `styles.css`; the
  only JS involvement is `isPhone()` in `app.js` for the three things CSS cannot
  decide — the calendar opens on **Day**, the size/scale steppers are hidden and
  their persisted values ignored (and **not** rewritten, so a narrowed desktop
  window can't clobber the size chosen there), and the day title shortens to
  "Tue, 4 Aug". Keep the breakpoint in sync between the two files. Two
  invariants:
  - **Nothing may make the document wider than the viewport.** A mobile browser
    answers horizontal overflow by *growing its layout viewport* to fit, which
    rescales the whole page and re-resolves `100vw` and every `position: fixed`
    overlay against the wider number. One unwrapped `.header-tools` row cost
    620px of layout on a 393px screen, and the visible symptom — the detail
    dialog hanging half off the left edge — was nowhere near the cause. This is
    why panels size off `calc(100% - 24px)` inside their fixed overlay rather
    than `94vw`, and why inputs are 16px on phones (below that, iOS zooms on
    focus and *stays* zoomed).
  - **Anything a finger taps is ≥44px** in its smaller dimension. Where 44px of
    visible control would wreck the silkscreen density, the box stays small and
    a transparent `::before` extends the hit area (`.task-check`).
  Touch also needs `pointercancel` handled wherever `pointerdown` starts a drag:
  a gesture the browser claims as a scroll never sends `pointerup`, so a
  listener attached on down stays attached. The chart selection and the month
  grid both got this wrong; the day/week grid's existing "tap, don't drag" guard
  is the pattern to copy. `touch-action: pan-y` on `.chart svg` is what keeps
  drag-to-select reachable with a finger without eating page scroll.
- Keep it a single lightweight web app served by the host — no separate build
  infrastructure beyond what's needed for a small SPA or server-rendered pages.
- Live-ish updates (short polling interval or simple WebSocket) rather than manual
  refresh, but nothing resource-heavy — this is still constrained by the 4GB Pi host.

## Conventions

- Keep firmware (`/firmware`) and host (`/server`, `/dashboard` or similar) cleanly
  separated in the repo.
- Comment the serial protocol wherever it's touched on either side — it's the seam most
  likely to drift out of sync between firmware and host if changed carelessly.
- Favor explicit, readable code over cleverness — this runs unattended 24/7; failures
  should be loud in logs, not silent.
- Don't add cloud dependencies, telemetry, or external services beyond what's already
  decided here (Tailscale, myStrom local API) without flagging it first.
