"""Auto-lighting job: a closed loop that holds the room's MEASURED illuminance
at the user's target (default 5 lx) by adjusting every bulb_zone currently in
'auto' mode. Deliberately its own thread — same reasoning as poller.py: this
event source is unrelated to the serial reader and the plug poller, and must
not block on (or be blocked by) either.

The control law, and the reasoning for it, lives in app/lighting_control.py
(pure, no IO). This file owns the three things that need the real world:

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
    """Adopt a live zone's brightness as the integrator's starting point."""
    global _brightness
    for zone in zones.values():
        try:
            state = zone.state()
        except BulbError:
            continue
        if state and state.get("brightness") is not None:
            _brightness = int(state["brightness"])
            log.info("Auto-lighting starting from the zone's current brightness %d/255",
                     _brightness)
            return
    log.info("No zone reachable at startup — auto-lighting starts from 0/255")


def _auto_loop() -> None:
    # Declared here, not in the branch below: `global` is a whole-function
    # declaration, and without it `status.get(...)` would raise
    # UnboundLocalError the moment the function also assigns to `status`.
    global _brightness, _last_change_at, _last_reading_ts, _step_scale, _last_sign, status
    consecutive_failures: dict[int, int] = {}
    suppressed_by: str | None = None
    while True:
        scene = _suppressing_scene()
        if scene != suppressed_by:  # log transitions once, not every tick
            if scene:
                log.info("Auto lighting paused — '%s' scene active", scene)
            else:
                log.info("Auto lighting resumed — house back in 'Home'")
            suppressed_by = scene
        if scene is None:
            auto_devices = {d["id"] for d in db.list_devices()
                            if d["type"] == "bulb_zone" and d.get("mode") == "auto"}
            if auto_devices:
                latest = db.latest_readings().get("lux")
                measured = latest["value"] if latest else None
                reading_ts = latest["ts"] if latest else None
                # Read every tick, not once at startup — the settings dialog
                # can move the target while the job runs.
                target_lux = db.get_lighting()["target_lux"]

                # Two DIFFERENT things, and conflating them was a bug:
                #
                # `unusable` — this sample cannot be corrected on. Either it
                #   predates our last brightness change by less than the settle
                #   time (so it does not show that change yet), or it is the
                #   same sample we already corrected on (correcting twice
                #   double-counts the error and overshoots). Entirely routine:
                #   the Arduino reports every 5 s, so at a faster poll interval
                #   most ticks land between samples. The loop holds — and the
                #   dashboard must keep showing the last real verdict, not
                #   "waiting for a reading", which reads like a fault.
                #
                # `sensor_lost` — the newest sample is genuinely old, so the
                #   BH1750 or the serial link has stopped. THAT is worth saying.
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

                # Between samples the room has not changed, so the last verdict
                # is still the true one — keep reporting it and only refresh the
                # measured number. A genuinely lost sensor does get reported.
                if result.state == lighting_control.STALE and not sensor_lost:
                    shown_state = status.get("state", result.state)
                    shown_detail = status.get("detail", "")
                else:
                    shown_state = result.state
                    shown_detail = lighting_control.describe(
                        shown_state, target_lux=target_lux, measured_lux=measured)
                if shown_state != status.get("state"):
                    log.info("Auto lighting: %s (brightness %d/255)",
                             shown_detail, result.brightness)
                status = {
                    "state": shown_state,
                    "detail": shown_detail,
                    "target_lux": target_lux,
                    "measured_lux": measured,
                    "brightness": result.brightness,
                }
                if result.brightness != _brightness:
                    _last_change_at = time.time()
                _brightness = result.brightness
                if not unusable and reading_ts is not None:
                    _last_reading_ts = reading_ts
                on, brightness = result.on, result.brightness
                with push_lock:
                    # Re-check under the lock: a scene may have activated
                    # since the top of this tick — its values must win, so
                    # this batch is dropped rather than pushed late.
                    if _suppressing_scene() is None:
                        for device_id in auto_devices:
                            zone = zones.get(device_id)
                            if zone is None:
                                continue
                            try:
                                zone.set_state(on=on, brightness=brightness)
                                if consecutive_failures.get(device_id):
                                    log.info("Bulb zone %d reachable again", device_id)
                                    consecutive_failures[device_id] = 0
                            except BulbError as exc:
                                n = consecutive_failures.get(device_id, 0) + 1
                                consecutive_failures[device_id] = n
                                # Loud on first failure, then once a few minutes, not every tick.
                                if n == 1 or n % 6 == 0:
                                    log.error("BULB ZONE UNREACHABLE (device %d, %d consecutive failures): %s",
                                              device_id, n, exc)
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
