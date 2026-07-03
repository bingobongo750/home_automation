"""Plug poller thread: samples the myStrom plug's state + power draw on its
own interval and logs it to SQLite. Deliberately independent of the serial
reader thread — the plug being down must never stall sensor ingestion, and
vice versa.
"""

import logging
import threading
import time

from . import config, db
from .mystrom import PlugError, make_plug

log = logging.getLogger("poller")

# device_id -> plug client, built at startup from the devices table.
plugs: dict[int, object] = {}


def _poll_loop() -> None:
    consecutive_failures: dict[int, int] = {}
    while True:
        for device_id, plug in plugs.items():
            try:
                report = plug.report()
                db.insert_power_reading(device_id, report["watts"], report["relay_on"])
                if consecutive_failures.get(device_id):
                    log.info("Plug device %d reachable again", device_id)
                    consecutive_failures[device_id] = 0
            except PlugError as exc:
                n = consecutive_failures.get(device_id, 0) + 1
                consecutive_failures[device_id] = n
                # Loud on first failure, then once a minute-ish, not every poll.
                if n == 1 or n % 6 == 0:
                    log.error("PLUG UNREACHABLE (device %d, %d consecutive failures): %s",
                              device_id, n, exc)
        time.sleep(config.MYSTROM_POLL_INTERVAL)


def start() -> threading.Thread:
    for device in db.list_devices():
        if device["type"] == "wifi_plug":
            plugs[device["id"]] = make_plug(device["ip"])
    log.info("Polling %d wifi plug(s) every %ss", len(plugs), config.MYSTROM_POLL_INTERVAL)
    thread = threading.Thread(target=_poll_loop, name="plug-poller", daemon=True)
    thread.start()
    return thread
