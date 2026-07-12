"""Auto-lighting job: on its own interval, pushes a brightness update to every
wled_zone device currently in 'auto' mode, based on the latest BH1750 lux
reading already sitting in the sensor DB. Deliberately its own thread — same
reasoning as poller.py: this event source is unrelated to the serial reader
and the plug poller, and must not block on (or be blocked by) either.

Devices in 'manual' mode are left alone; the dashboard drives those directly
through /api/devices/:id/state.

House modes: while a scene other than 'Day' is active (see app/scenes.py),
the whole job is suppressed — the scene's explicit zone states win, and zones
keep their 'auto' mode column so returning to Day resumes lux control without
any re-configuration. Scene activation calls poke() so the loop reacts
immediately instead of waiting out the current sleep interval.
"""

import logging
import threading
import time

from . import config, db
from .wled import WledError, make_wled_zone

log = logging.getLogger("lighting")

# device_id -> WLED client, built at startup from the devices table.
zones: dict[int, object] = {}

# Serializes zone pushes between the auto loop and scene application
# (app/scenes.py). Without it, an auto tick already in flight when a scene
# activates could land its on/brightness push AFTER the scene's explicit
# values — and with the loop suppressed from then on, a zone the scene turned
# off would stay lit until the next scene change.
push_lock = threading.Lock()

_poke = threading.Event()


def poke() -> None:
    """Wake the auto loop now — a switch back to Day re-applies lux-driven
    brightness immediately, and a switch away pauses the job immediately."""
    _poke.set()


def _suppressing_scene() -> str | None:
    """Name of the active scene suppressing auto mode, or None. Any scene
    other than 'Day' pins zones to its explicit values — the auto job must
    not fight it. No scene ever activated counts as 'Day'."""
    active = db.get_active_scene()
    if active and active["name"] != "Day":
        return active["name"]
    return None


def _desired_state(lux: float | None) -> tuple[bool, int]:
    """-> (on, brightness) for the given lux reading. Below the threshold the
    room counts as dark and the zone lights up to LIGHTING_AUTO_BRIGHTNESS;
    at/above it the zone turns off."""
    if lux is None or lux < config.LIGHTING_LUX_THRESHOLD:
        return True, config.LIGHTING_AUTO_BRIGHTNESS
    return False, 0


def _auto_loop() -> None:
    consecutive_failures: dict[int, int] = {}
    suppressed_by: str | None = None
    while True:
        scene = _suppressing_scene()
        if scene != suppressed_by:  # log transitions once, not every tick
            if scene:
                log.info("Auto lighting paused — '%s' scene active", scene)
            else:
                log.info("Auto lighting resumed — house back in 'Day'")
            suppressed_by = scene
        if scene is None:
            auto_devices = {d["id"] for d in db.list_devices()
                            if d["type"] == "wled_zone" and d.get("mode") == "auto"}
            if auto_devices:
                latest = db.latest_readings().get("lux")
                on, brightness = _desired_state(latest["value"] if latest else None)
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
                                    log.info("WLED zone %d reachable again", device_id)
                                    consecutive_failures[device_id] = 0
                            except WledError as exc:
                                n = consecutive_failures.get(device_id, 0) + 1
                                consecutive_failures[device_id] = n
                                # Loud on first failure, then once a few minutes, not every tick.
                                if n == 1 or n % 6 == 0:
                                    log.error("WLED ZONE UNREACHABLE (device %d, %d consecutive failures): %s",
                                              device_id, n, exc)
        _poke.wait(config.LIGHTING_POLL_INTERVAL)
        _poke.clear()


def start() -> threading.Thread:
    for device in db.list_devices():
        if device["type"] == "wled_zone":
            zones[device["id"]] = make_wled_zone(device["ip"])
    log.info("Auto-lighting job covering %d WLED zone(s), checking every %ss",
              len(zones), config.LIGHTING_POLL_INTERVAL)
    thread = threading.Thread(target=_auto_loop, name="lighting-auto", daemon=True)
    thread.start()
    return thread
