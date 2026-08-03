# Presence — automatic Away, and the disturbance summary

Design for phone-driven presence: leaving the house puts it in **Away**, coming
back puts it in **Day** or **Sleeping** depending on the time, and the return
shows a summary of anything that happened while nobody was in.

Status: **built.** `app/presence.py`, the `_fire_bedtime` Away guard, the away
summary and its dashboard card are all implemented and covered by
`server/tests/test_presence.py`.

Still outstanding from this document: **§9's Day and Sleeping halves** — motion
is not yet suppressed on the Board during Day, and the overnight summary still
counts raw motion rows rather than clustered awakenings. §9's Away half is done,
since the away summary uses the clustering.

---

## 1. Principles

**The phone reports presence. The host decides the scene.** The Shortcut sends
"I left" / "I came back" and nothing else. Which scene that becomes, whether it
is night, whether a summary is worth showing — all host-side. The phone has no
copy of the sleep schedule to drift out of sync with, and changing the schedule
never means editing a Shortcut. This is the same split as the serial protocol,
where the Arduino reports readings and the host decides what they mean.

**Away is the strongest state.** Nothing automatic may override it. Not the
nightly bedtime timer, not a pending wake. Only an arrival, or the user picking
a scene by hand, ends Away.

**Departure is delayed; arrival is immediate.** A geofence that bounces must not
strobe the room, so a departure is confirmed after a grace period. Arrival has
no grace — the point is that lights are on when you walk in.

**Arrival only ever ends Away.** If the house is in Day or Sleeping, an arrival
is a no-op. This is what stops a spurious geofence event from overriding a scene
you chose deliberately.

---

## 2. The two Shortcuts

Both are Personal Automations under **Location**, not Wi-Fi. A geofence still
works when Wi-Fi is off and does not fire on a router hiccup. Both need
**"Ask Before Running" off**.

### Departure

```
Trigger:  Location → Leave → <home>
Actions:  1. Wait  30 seconds
          2. Get Contents of URL
               URL     http://hub:8000/api/presence/departed
               Method  POST
```

The wait exists because at the moment you leave, the phone is handing off from
Wi-Fi to cellular and Tailscale has not necessarily re-established. Firing
immediately is the single most likely way for this request to be lost.

### Arrival

```
Trigger:  Location → Arrive → <home>
Actions:  1. Get Contents of URL
               URL     http://hub:8000/api/presence/arrived
               Method  POST
```

No wait — you are walking through the door.

Neither action needs a request body, which keeps each Shortcut to one action.

> **This requires Tailscale on the phone.** Once off home Wi-Fi, `hub:8000`
> resolves only over the tailnet. A departure fired with Tailscale off is simply
> lost.

**If a request is lost**, nothing dangerous happens: a lost departure leaves the
house in Day while you are out; a lost arrival means you come home to an Away
house and fix it with the dashboard MODE switch. Shortcuts does not retry, and
adding retry logic on the phone is not worth it — neither failure is harmful.

---

## 3. Endpoints

```
POST /api/presence/departed   → confirm-and-arm; no body
POST /api/presence/arrived    → act now; no body
GET  /api/presence            → { state, since, pending_departure_at }
```

Two verbs rather than one endpoint with a body, purely so each Shortcut is a
bare URL. Responses say what actually happened, so the Shortcut can surface a
notification if wanted:

```json
{ "presence": "away",
  "scene": "Away",
  "applied": true,
  "reason": "departure confirmed after grace period" }
```

```json
{ "presence": "home",
  "scene": "Sleeping",
  "applied": true,
  "reason": "arrived inside the nightly sleep window",
  "summary_generated": true }
```

No authentication, consistent with the rest of the API — Tailscale is the
boundary (`CLAUDE.md`). Do not add a shared secret to these two endpoints alone.

---

## 4. State machine

Presence is persisted in `settings` under `presence`:

```json
{ "state": "home" | "away",
  "since": 1785790430.3,
  "last_event": "arrived",
  "last_event_at": 1785790430.3 }
```

It survives restarts, like the active scene. On startup, a pending departure
grace timer is **not** restored — if the hub was down through your departure,
re-arming a stale timer to darken the house minutes later is worse than doing
nothing.

### Departure

| Current scene | Behaviour |
|---|---|
| Day | Arm the grace timer. On expiry → Away. |
| Sleeping | Arm the grace timer. On expiry → Away. (Activating Away already cancels the pending wake.) |
| Away | No-op. Keep the original `since`, so the summary window still covers the whole absence. |

A second `departed` while a grace timer is already armed is a no-op — it does
**not** restart the countdown.

### Arrival

| Current scene | Behaviour |
|---|---|
| Away | → **Day** or **Sleeping** by the rule in §5. Compute the away summary. |
| Day | No-op. |
| Sleeping | No-op. |
| *(grace timer armed, Away not yet applied)* | Cancel the timer. Nothing was applied, so nothing to undo. This is the bounce case, and it is silent. |

Arrival never touches a scene it did not set. That is the whole protection
against a flaky geofence stomping on a deliberate choice.

---

## 5. Day or Sleeping on arrival

Read the stored schedule (`db.get_sleep_schedule()` — `enabled`, `sleep_time`,
`wake_time`, local `"HH:MM"`).

```
if not enabled                        → Day
if sleep_time == wake_time            → Day        (zero-length window)
if sleep_time <  wake_time            → Sleeping when sleep_time <= now < wake_time
if sleep_time >  wake_time (wraps)    → Sleeping when now >= sleep_time or now < wake_time
```

The wrap case is not hypothetical — the default is `00:00 → 09:30`, which does
not wrap, but any bedtime after midnight-minus-one does.

**Arriving into the sleep window activates Sleeping carrying the schedule's own
`wake_time`**, exactly as `_fire_bedtime()` does. That way the existing
Sleeping→Day machinery still runs in the morning and you still get the overnight
summary. Getting home at 02:00 must not cost you the morning report.

---

## 6. Required change: Away must survive the night

`scenes._fire_bedtime()` currently activates Sleeping unconditionally. Away
would therefore be overwritten at bedtime, and the morning wake would then put
the house into Day while you are still gone — lights and plugs on in an empty
flat, which is the exact opposite of the intent.

**Fix:** `_fire_bedtime()` skips when the active scene is `Away`, logs that it
did, and still calls `reschedule_bedtime()` so tomorrow is armed.

This matches the precedent already in the file: a restart mid-window
deliberately does not back-fill, "so coming back up at 02:00 never overrides a
house someone deliberately put in Away."

No change is needed to the wake timer — activating Away already cancels it.

---

## 7. The away summary

Computed once, at the Away→(Day|Sleeping) transition. Stored in `settings` under
`last_away_summary`; served by `GET /api/scenes/last-away-summary`. Same shape of
machinery as the overnight summary, deliberately **different content**: that one
is about sleep quality, this one is about whether anything happened in an empty
room.

Skip generation entirely when the absence was shorter than
`PRESENCE_SUMMARY_MIN_S` (default 10 min) — nobody needs a report on a trip to
the bins.

### The window is trimmed at the end, and that matters

```
away_since  ──────────────── summary window ──────────────┤    ┆
                                                          │    ┆
                                    arrival_at − TRIM ─────┘    ┆
                                                   arrival_at ──┘
```

You reach the door before the hub knows you are back. The geofence has a radius,
iOS takes its time running the automation, the request has to cross the network.
Everything the sensors record in that gap is **you** — the PIR firing as you walk
in, CO2 rising as you breathe, lux jumping as you hit the light switch, a plug
drawing current as you switch something on. Reported as "disturbances", it would
make every single homecoming look like a break-in, and a summary that always
cries wolf is one you stop reading.

So the window ends at `arrival_at − PRESENCE_ARRIVAL_TRIM_S` (**default 120 s**).
The trim applies to the **whole window**, not just motion — every signal above is
contaminated the same way.

Two minutes is deliberately generous. The real latency is probably far shorter,
but the costs are asymmetric: trimming too much loses the last two minutes of an
absence that was already hours long, while trimming too little produces a false
alarm on every arrival. Err long.

**No trim is needed at the start.** `away_since` is when Away was *applied*,
which is already `PRESENCE_DEPART_GRACE_S` after the departure event, which is
itself 30 s after the geofence fired. You have been gone a couple of minutes
before the window even opens.

If the trim would leave a window shorter than `PRESENCE_SUMMARY_MIN_S`, no
summary is generated at all.

```json
{
  "from": 1785790430.3,
  "to": 1785801230.9,
  "duration_s": 10800,
  "disturbed": true,
  "motion": { "count": 4, "first": 1785793000.0, "last": 1785793120.0,
              "events": [ ... capped at 20 ... ] },
  "co2":    { "max": 780, "start": 610, "end": back, "rose_significantly": true },
  "lux":    { "max": 410, "peaked_at": 1785793010.0 },
  "temp":   { "min": 21.1, "max": 24.8, "avg": 22.4 },
  "hum":    { "min": 39.0, "max": 52.3, "avg": 44.1 },
  "plugs":  [ { "name": "Plug 1", "max_watts": 61.2, "changed_state": true } ]
}
```

**`disturbed`** is the one field the dashboard leads on. True when at least one
motion *event* occurred (see §8), **or** CO2 rose by `CO2_RISE_FLAG_PPM`,
**or** a plug's relay state differs from departure. The reassuring case —
"nothing happened" — must be as clear as the alarming one.

Why CO2 as well as the PIR: a person sitting still defeats a PIR, which senses
heat *movement across* its field. They cannot defeat CO2 — a human in a closed
room raises it measurably within minutes. The two signals fail in different
ways, which is exactly why both are worth having. (Moot until the SCD40 is
replaced; the field should be omitted rather than shown as zero when no valid
CO2 exists in the window.)

Everything here comes from `readings` and `power_readings`, which are already
being written. No new tables, no new collection — the same "compute at the
transition, do not build a second report system" decision the overnight summary
made.

### Dashboard

A dismissible card at the top of **Room conditions**, matching the overnight
summary's pattern, with per-summary dismissal in `localStorage`. Shown whenever
an undismissed away summary exists, regardless of the current scene — arriving
at 02:00 into Sleeping should still leave it waiting for you in the morning.

If both an overnight and an away summary are undismissed, show the away one on
top: it is the one that might need action.

---

## 8. Events, not samples — the disturbance cooldown

A PIR does not produce "a detection". It produces a `1` every reporting cycle
for as long as it keeps seeing movement, so one person crossing the room is
dozens of rows. Counting rows would report "47 disturbances" for a single event,
which tells you nothing and buries the distinction that matters — **one
disturbance versus several separate ones**.

So raw samples are collapsed into **events** before anything counts them:

> Consecutive detections separated by less than `DISTURBANCE_COOLDOWN_S`
> (**default 300 s**) are one event. A gap of at least that long starts a new one.

One helper, `_cluster_events(timestamps, gap_s) -> list[(start, end)]`, used
everywhere a repeated signal is counted:

| Signal | Why it needs clustering |
|---|---|
| PIR motion | dozens of samples per crossing |
| Plug relay transitions | anything thermostatic (a fridge) cycles all day on its own |

CO2, lux, temperature and humidity are **not** clustered — they are levels, not
events, and are already reported as max/min/delta rather than counts.

Five minutes is chosen so that leaving the room and coming back still reads as
one event. It is deliberately the same constant for both scenes below; split it
only if the two genuinely want different values.

---

## 9. Scene-aware motion policy

The PIR means something different in each scene, so each scene treats it
differently.

| Scene | Recorded | Shown on the Board | Counted as |
|---|---|---|---|
| **Day** | yes | **suppressed** | — |
| **Sleeping** | yes | yes | **awakenings** — times you got out of bed |
| **Away** | yes | yes | **movement events** — the primary disturbance signal |

### Recording never stops

Every sample is stored in every scene. This is the same rule as the CO2
plausibility band, for a different reason: filtering CO2 hid a *hardware fault*,
whereas suppressing motion during Day hides nothing broken — but it would make
the stored record silently scene-dependent, so "was anyone in the room at 15:00
last Tuesday?" could no longer be answered. Suppression is a **presentation**
decision and belongs at read time.

There is no storage argument for dropping it either: `hub_node.ino` emits
`MOTION:` on every report cycle regardless of value (plus immediately on change),
so the row count is identical whether the PIR is firing or idle.

### Day — suppressed, and visibly so

While Day is active, the motion widget and the activity log ignore motion
readings. The widget reads **"Motion — paused (Day)"** rather than showing a
false "no motion", mirroring the existing `Auto paused — ‹scene› scene active`
wording on auto-mode lighting cards. A control that is off must say so; one that
silently reports nothing is worse than one that reports noise.

### Sleeping — awakenings

Motion events during the Sleeping window are counted as **times you got out of
bed** and added to the overnight summary beside the existing temp/hum/CO2 stats.
Report the count and each event's start time — waking at 03:00 and at 07:20 are
very different nights.

> **This changes the existing overnight summary.** It currently records a raw
> motion *count* and event times; that count is dominated by however long the PIR
> stayed high and is not comparable night to night. It becomes a clustered event
> count.

> **Do not merge this with the Health module's `Awakenings` sub-score.** That one
> comes from Apple Health sleep stages — the watch detecting a wake stage. The
> PIR counts times you physically left the bed area. You can wake without getting
> up, so the PIR number is a strict subset and a different measurement. They are
> complementary; correlating them later might be interesting, conflating them now
> would corrupt a scored input.

### Away — kept, obviously

Unchanged from §7: motion is the headline disturbance signal.

### Hardware note

If the PIR really is high most of the time with you in the room, some of that is
tunable at the sensor. The HC-SR501 has a **delay potentiometer** (how long the
output stays high after a detection — anywhere from ~3 s to ~5 min) and a
**retrigger jumper** (`H` = repeatable, restarting the delay on every detection).
Set long and repeatable, it latches high almost permanently with an occupant.
Turning the delay to minimum reduces the noise at source, which is better than
filtering it later. Measured on the Pi so far: high on **15 %** of samples.

---

## 10. Configuration

```
PRESENCE_DEPART_GRACE_S=120     # confirm a departure before acting
PRESENCE_ARRIVAL_TRIM_S=120     # ignore the run-up to a detected arrival
PRESENCE_SUMMARY_MIN_S=600      # shorter absences generate no summary
DISTURBANCE_COOLDOWN_S=300      # collapse repeated detections into one event
```

All in `config.py` with defaults, all documented in `.env.example` in the same
commit — a variable added on the Mac and forgotten on the Pi falls back
silently (`MANUAL.md` §8.2).

---

## 11. Build order

1. `config.py` + `.env.example` — the two variables.
2. `scenes._fire_bedtime()` — skip when Away. Smallest change, and it is
   correct on its own even if nothing else here is built.
3. `db` helpers — `get_presence()` / `set_presence()`, matching the existing
   settings accessors.
4. `presence.py` — the state machine and the grace timer, reusing the
   generation-counter pattern already used for the wake and bedtime timers.
   Import direction is `presence → scenes`, never the reverse.
5. `_compute_away_summary()` in `scenes.py`, beside the sleep one, and the
   `last-away-summary` endpoint.
6. Endpoints in `api.py`.
7. `dashboard/` — the summary card.
8. `server/tests/test_presence.py` — the case table in §4 is the test list.

Steps 1–2 are independently useful and could ship alone.

## 12. Cases the tests must cover

- depart → grace → Away; arrive during grace → nothing applied, timer cancelled
- depart while Sleeping → Away, pending wake cancelled
- double depart → one timer, `since` unchanged
- arrive while Day → no-op; arrive while Sleeping → no-op
- arrive inside the sleep window → Sleeping **with** the schedule's wake_time
- arrive outside it → Day
- wrapping sleep window (e.g. `23:00 → 07:00`) on both sides of midnight
- schedule disabled → always Day
- bedtime timer fires while Away → skipped, tomorrow still armed
- absence under `PRESENCE_SUMMARY_MIN_S` → no summary stored
- summary window with no motion → `disturbed: false`
- restart with a departure pending → timer not restored
