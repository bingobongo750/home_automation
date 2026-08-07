"""Auto-lighting job: a closed loop that holds the room's MEASURED illuminance
at the user's target (default 5 lx) by adjusting every bulb_zone currently in
'auto' mode. Deliberately its own thread — same reasoning as poller.py: this
event source is unrelated to the serial reader and the plug poller, and must
not block on (or be blocked by) either.

The control law, and the reasoning for it, lives in app/lighting_control.py
(pure, no IO). This file owns the things that need the real world:

0. THE ON/OFF SWITCH IS A MASTER GATE, AND IT BEATS THE LOOP — but the gate is
   a STORED SETTING, not the bulb's current on/off. Those are two different
   questions and conflating them was the bug this design fixes:

     gate  (devices.switch_on)  "may this lamp light at all?"   — the user's
     lit   (the bulb's `on`)    "is it lit right now?"          — the loop's

   The user owns the gate; nothing automatic writes it. Within an armed zone
   the loop owns both brightness AND the physical on/off, so when it wants zero
   light it switches the bulb off outright instead of bottoming it out at the
   Shelly's 1 % floor. A zone whose gate is off is skipped entirely and is
   never lit.

   That is what makes the dashboard switch mean "armed": it stays ON through a
   bright afternoon with the lamp physically dark, so you can see at a glance
   which lamps will come up when the room dims. The earlier design read the
   gate straight off the bulb, which is why the loop could not touch `on` — it
   would have been overwriting the very signal it was reading, and the first
   version of that bug pushed `on=True` every tick so switching a zone off
   undid itself seconds later. Storing the gate separates them.

   When NO auto zone is armed, the loop holds its integrator instead of
   integrating against a room it cannot affect; otherwise it would wind up to
   full while blind and blast the room the moment a zone was armed again.

1. ONE CONTROLLER FOR ALL ZONES, not one per zone. There is a single BH1750 and
   a single room. Two independent controllers reading the same sensor would
   each see the other's light, each conclude it was still short of target, and
   both keep pushing — the combined output overshoots roughly two-fold and
   then they fight on the way back down. So the loop computes ONE brightness
   and commands it to every auto zone. If this hub ever covers more than one
   room, this is the thing that has to change first: a controller per sensor.

2. NEVER CORRECT TWICE ON THE SAME MEASUREMENT. After changing brightness, the
   next reading must be new enough to describe the change (LIGHTING_SETTLE_S,
   against the reading's own timestamp — the Arduino only reports every 5 s).
   Without this the loop integrates the same error repeatedly and overshoots
   every time; it is the single cheapest thing that keeps it stable. Note the
   settle clock restarts on a brightness CHANGE, not on every push: the loop
   re-asserts its current value each tick (so a zone knocked out of sync by a
   manual tap comes back), and treating those as changes would keep the guard
   permanently armed and the loop permanently blind.

3. Reporting saturation. "The room is already brighter than the target" is a
   normal outcome, not an error — `status` below is what the dashboard reads to
   say so out loud instead of appearing to do nothing.

Devices in 'manual' mode are left alone; the dashboard drives those directly
through /api/devices/:id/state.

House modes: while a scene other than 'Home' is active (see app/scenes.py),
the whole job is suppressed — the scene's explicit zone states win, and zones
keep their 'auto' mode column so returning to Home resumes control without any
re-configuration. Scene activation calls poke() so the loop reacts immediately
instead of waiting out the current sleep interval.
"""

import logging
import threading
import time

from . import config, db, lighting_control
from .shelly_bulb import BulbError, make_bulb

log = logging.getLogger("lighting")

# device_id -> Shelly bulb client, built at startup from the devices table.
zones: dict[int, object] = {}

# Serializes zone pushes between the auto loop and scene application
# (app/scenes.py). Without it, an auto tick already in flight when a scene
# activates could land its on/brightness push AFTER the scene's explicit
# values — and with the loop suppressed from then on, a zone the scene turned
# off would stay lit until the next scene change.
push_lock = threading.Lock()

_poke = threading.Event()


def poke() -> None:
    """Wake the auto loop now — a switch back to Home re-applies lux-driven
    brightness immediately, and a switch away pauses the job immediately."""
    _poke.set()


def commanded_brightness() -> int:
    """The controller's current integrator — the brightness an armed zone should
    be lit at right now, or 0 when it wants the room dark.

    The /state endpoint reads it when the user arms a zone, so the lamp comes up
    at the right level immediately rather than flashing on at a stale value and
    being corrected a tick later — or, when the loop wants darkness, does not
    flash on at all."""
    return _brightness


def _suppressing_scene() -> str | None:
    """Name of the active scene suppressing auto mode, or None. Any scene
    other than 'Home' pins zones to its explicit values — the auto job must
    not fight it. No scene ever activated counts as 'Home'."""
    active = db.get_active_scene()
    if active and active["name"] != "Home":
        return active["name"]
    return None


# Live controller state, for GET /api/devices (bulb rows) and the dashboard
# card. Rebuilt every tick; read without a lock because it is replaced
# wholesale, never mutated in place, so a reader always sees one consistent
# snapshot rather than a half-updated dict.
status: dict = {
    "state": lighting_control.NO_READING,
    "detail": "Auto lighting has not run yet.",
    "target_lux": config.LIGHTING_TARGET_LUX,
    "measured_lux": None,
    "brightness": 0,
    # Whether the loop currently wants its armed zones lit at all. False means
    # the room is already bright enough and the bulbs are off — an armed zone
    # still shows its switch ON, so this is what tells the card apart from
    # "switched off by the user".
    "lit": False,
}

# The controller's integrator: the brightness we last commanded. Seeded at
# startup from what a zone reports, so a restart does not drop the room to dark
# and spend several ticks climbing back.
_brightness = 0
# Wall clock of the last brightness CHANGE, and the reading timestamp already
# acted on — the two halves of "never correct twice on the same measurement".
_last_change_at = 0.0
_last_reading_ts = None
# Carried controller state. Must survive between ticks: without it the loop
# limit-cycles on a bright lamp close to the sensor (see point 3 of the
# app/lighting_control.py docstring — the tests pin the regression).
_step_scale = 1.0
_last_sign = 0


def _seed_brightness() -> None:
    """Adopt a LIT zone's brightness as the integrator's starting point, so a
    restart does not drop the room to dark and spend several ticks climbing back.

    Only a lit zone counts. A bulb keeps its brightness attribute while switched
    off, so an armed-but-dark zone still reports the level it last shone at —
    and that is precisely the level the loop had decided against. Seeding from it
    would flash the lamps on after every restart in a bright room and take a
    string of ticks to walk back down. Armed and dark means the controller wanted
    darkness: start from 0 and let it climb if the room disagrees."""
    global _brightness
    armed_seen = False
    for device in db.list_devices():
        if device["type"] != "bulb_zone" or not db.device_switch_on(device):
            continue
        zone = zones.get(device["id"])
        if zone is None:
            continue
        try:
            state = zone.state()
        except BulbError:
            continue
        if not state:
            continue
        armed_seen = True
        if state.get("on") and state.get("brightness") is not None:
            _brightness = int(state["brightness"])
            log.info("Auto-lighting starting from the zone's current brightness %d/255",
                     _brightness)
            return
    _brightness = 0
    if armed_seen:
        log.info("Armed zones are all dark — auto-lighting starts from 0/255")
    else:
        log.info("No armed zone reachable at startup — auto-lighting starts from 0/255")


# Per-tick bookkeeping that used to be locals of the loop. Module-level so the
# tick can be called on its own — the body is long enough that it needs testing
# without spinning up a thread (see tests/test_lighting.py).
_consecutive_failures: dict[int, int] = {}
_suppressed_by: str | None = None


def _one_tick() -> None:
    """One pass of the auto-lighting loop. Returns early whenever there is
    nothing to do, so the thread below is just `while True: tick; sleep`."""
    global _brightness, _last_change_at, _last_reading_ts, _step_scale, _last_sign
    global status, _suppressed_by

    scene = _suppressing_scene()
    if scene != _suppressed_by:  # log transitions once, not every tick
        if scene:
            log.info("Auto lighting paused — '%s' scene active", scene)
        else:
            log.info("Auto lighting resumed — house back in 'Home'")
        _suppressed_by = scene
    if scene is not None:
        return

    auto_devices = [d for d in db.list_devices()
                    if d["type"] == "bulb_zone" and d.get("mode") == "auto"]
    if not auto_devices:
        return

    latest = db.latest_readings().get("lux")
    measured = latest["value"] if latest else None
    reading_ts = latest["ts"] if latest else None
    # Read every tick, not once at startup — the settings dialog can move the
    # target while the job runs.
    target_lux = db.get_lighting()["target_lux"]

    # Which auto zones the user has ARMED. Read from the stored gate, not from
    # the bulbs: the loop switches its own zones off when the room is bright
    # enough, so the bulb's `on` answers "is it lit", not "may it light" (see
    # point 0 above). Reading it back here would make the loop treat its own
    # switch-off as the user disarming the zone, and it would never come back.
    live = {d["id"]: zones[d["id"]] for d in auto_devices
            if db.device_switch_on(d) and d["id"] in zones}

    if not live:
        # Nothing to drive. Hold the integrator instead of letting it wind up
        # against a room it cannot affect, or flipping a switch back on would
        # blast whatever it had wound up to.
        detail = lighting_control.describe(
            lighting_control.ZONES_OFF, target_lux=target_lux, measured_lux=measured)
        if status.get("state") != lighting_control.ZONES_OFF:
            log.info("Auto lighting: %s", detail)
        status = {
            "state": lighting_control.ZONES_OFF,
            "detail": detail,
            "target_lux": target_lux,
            "measured_lux": measured,
            "brightness": _brightness,
            "lit": False,
        }
        return

    # Two DIFFERENT things, and conflating them was a bug:
    #
    # `unusable` — this sample cannot be corrected on. Either it predates our
    #   last brightness change by less than the settle time (so it does not show
    #   that change yet), or it is the same sample we already corrected on
    #   (correcting twice double-counts the error and overshoots). Entirely
    #   routine: the Arduino reports every 5 s, so at a faster poll interval most
    #   ticks land between samples. The loop holds — and the dashboard must keep
    #   showing the last real verdict, not "waiting for a reading", which reads
    #   like a fault.
    #
    # `sensor_lost` — the newest sample is genuinely old, so the BH1750 or the
    #   serial link has stopped. THAT is worth saying.
    sensor_lost = (reading_ts is not None
                   and time.time() - reading_ts > config.LIGHTING_STALE_AFTER_S)
    fresh = (reading_ts is not None
             and reading_ts >= _last_change_at + config.LIGHTING_SETTLE_S
             and (_last_reading_ts is None or reading_ts > _last_reading_ts))
    unusable = sensor_lost or not fresh

    result = lighting_control.correct(
        measured_lux=measured,
        target_lux=target_lux,
        brightness=_brightness,
        max_brightness=config.LIGHTING_AUTO_BRIGHTNESS,
        deadband_lux=config.LIGHTING_DEADBAND_LUX,
        gain=config.LIGHTING_GAIN,
        max_step=config.LIGHTING_MAX_STEP,
        step_scale=_step_scale,
        last_sign=_last_sign,
        reading_stale=unusable,
    )
    _step_scale, _last_sign = result.step_scale, result.sign

    # Zero brightness means the room needs no help from us, so switch the lamps
    # off rather than leave them glowing at the Shelly's 1 % floor. The gate is
    # stored, so this is ours to do and does not disarm the zone — the card's
    # switch stays ON and says the lamp is waiting for the room to dim.
    lit = result.brightness > 0

    # Between samples the room has not changed, so the last verdict is still the
    # true one — keep reporting it and only refresh the measured number. A
    # genuinely lost sensor does get reported.
    if result.state == lighting_control.STALE and not sensor_lost:
        shown_state = status.get("state", result.state)
        shown_detail = status.get("detail", "")
    else:
        shown_state = result.state
        shown_detail = lighting_control.describe(
            shown_state, target_lux=target_lux, measured_lux=measured, lit=lit)
    if shown_state != status.get("state"):
        log.info("Auto lighting: %s (brightness %d/255)", shown_detail, result.brightness)
    status = {
        "state": shown_state,
        "detail": shown_detail,
        "target_lux": target_lux,
        "measured_lux": measured,
        "brightness": result.brightness,
        "lit": lit,
    }

    # target_lux 0 means "auto lighting is off" — make no changes at all, rather
    # than switching every armed zone off. "Disabled" has to mean hands off, or
    # it would be indistinguishable from auto lighting deciding the room is
    # bright enough.
    if result.state == lighting_control.OFF_BY_SETTING:
        return

    if result.brightness != _brightness:
        _last_change_at = time.time()
    _brightness = result.brightness
    if not unusable and reading_ts is not None:
        _last_reading_ts = reading_ts

    with push_lock:
        # Re-check under the lock: a scene may have activated since the top of
        # this tick — its values must win, so this batch is dropped rather than
        # pushed late.
        if _suppressing_scene() is not None:
            return
        for device_id, zone in live.items():
            try:
                # Inside an ARMED zone the loop owns both. Brightness is only
                # sent when the lamp is to be lit — at zero it would floor to
                # the Shelly's 1 %, which is the glow we are switching off.
                if lit:
                    zone.set_state(on=True, brightness=result.brightness)
                else:
                    zone.set_state(on=False)
                if _consecutive_failures.get(device_id):
                    log.info("Bulb zone %d reachable again", device_id)
                    _consecutive_failures[device_id] = 0
            except BulbError as exc:
                n = _consecutive_failures.get(device_id, 0) + 1
                _consecutive_failures[device_id] = n
                # Loud on first failure, then once a few minutes, not every tick.
                if n == 1 or n % 6 == 0:
                    log.error("BULB ZONE UNREACHABLE (device %d, %d consecutive failures): %s",
                              device_id, n, exc)


def _auto_loop() -> None:
    while True:
        try:
            _one_tick()
        except Exception:                      # noqa: BLE001
            # A 24/7 thread must not die on one bad tick — log it and try again
            # next interval. Without this, one unexpected exception silently
            # ends auto lighting until the next restart.
            log.exception("Auto-lighting tick failed; continuing")
        _poke.wait(config.LIGHTING_POLL_INTERVAL)
        _poke.clear()


def start() -> threading.Thread:
    for device in db.list_devices():
        if device["type"] == "bulb_zone":
            zones[device["id"]] = make_bulb(device["ip"])
    _seed_brightness()
    log.info("Auto-lighting job covering %d bulb zone(s), checking every %ss, "
             "holding %.0f lx (+/-%.1f, gain %.1f, max step %d/255)",
             len(zones), config.LIGHTING_POLL_INTERVAL, db.get_lighting()["target_lux"],
             config.LIGHTING_DEADBAND_LUX, config.LIGHTING_GAIN, config.LIGHTING_MAX_STEP)
    thread = threading.Thread(target=_auto_loop, name="lighting-auto", daemon=True)
    thread.start()
    return thread
