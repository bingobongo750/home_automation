"""Auto-lighting job: on its own interval, pushes a brightness update to every
wled_zone device currently in 'auto' mode, based on the latest BH1750 lux
reading already sitting in the sensor DB. Deliberately its own thread — same
reasoning as poller.py: this event source is unrelated to the serial reader
and the plug poller, and must not block on (or be blocked by) either.

Devices in 'manual' mode are left alone; the dashboard drives those directly
through /api/devices/:id/state.
"""

import logging
import threading
import time

from . import config, db
from .wled import WledError, make_wled_zone

log = logging.getLogger("lighting")

# device_id -> WLED client, built at startup from the devices table.
zones: dict[int, object] = {}


def _desired_state(lux: float | None) -> tuple[bool, int]:
    """-> (on, brightness) for the given lux reading. Below the threshold the
    room counts as dark and the zone lights up to LIGHTING_AUTO_BRIGHTNESS;
    at/above it the zone turns off."""
    if lux is None or lux < config.LIGHTING_LUX_THRESHOLD:
        return True, config.LIGHTING_AUTO_BRIGHTNESS
    return False, 0


def _auto_loop() -> None:
    consecutive_failures: dict[int, int] = {}
    while True:
        auto_devices = {d["id"] for d in db.list_devices()
                        if d["type"] == "wled_zone" and d.get("mode") == "auto"}
        if auto_devices:
            latest = db.latest_readings().get("lux")
            on, brightness = _desired_state(latest["value"] if latest else None)
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
        time.sleep(config.LIGHTING_POLL_INTERVAL)


def start() -> threading.Thread:
    for device in db.list_devices():
        if device["type"] == "wled_zone":
            zones[device["id"]] = make_wled_zone(device["ip"])
    log.info("Auto-lighting job covering %d WLED zone(s), checking every %ss",
              len(zones), config.LIGHTING_POLL_INTERVAL)
    thread = threading.Thread(target=_auto_loop, name="lighting-auto", daemon=True)
    thread.start()
    return thread
