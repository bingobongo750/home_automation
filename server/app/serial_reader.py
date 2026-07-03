"""Serial reader thread: Arduino KEY:VALUE lines -> SQLite.

SERIAL PROTOCOL (keep in sync with /docs/serial-protocol.md and
/firmware/hub_node/hub_node.ino):

  Arduino -> host, one reading per line:  TEMP:21.4  HUM:47.2  LUX:312
  CO2:612  MOTION:1.  Lines starting with '#' are firmware logs — passed
  through to our log at DEBUG, never stored.

  Host -> Arduino: send_command() writes a single "KEY:VALUE\n" line
  (RELAY1:ON, DIM1:180, MODE:aqi, COLOR:255,0,0).

The thread reconnects with a fixed backoff if the port is missing or drops
(USB unplugged), logging loudly each time rather than crashing the app.
With MOCK_HARDWARE=1 a fake data generator replaces the port entirely.
"""

import logging
import math
import random
import threading
import time

from . import config, db

log = logging.getLogger("serial")

# Serial KEY -> DB metric name. Unknown keys are logged and dropped.
KEY_TO_METRIC = {
    "TEMP": "temp",
    "HUM": "hum",
    "LUX": "lux",
    "CO2": "co2",
    "MOTION": "motion",
}

RECONNECT_DELAY_S = 5

_port = None          # live serial.Serial handle, or None
_port_lock = threading.Lock()


def handle_line(line: str) -> None:
    """Parse one line from the Arduino and store it if it's a reading."""
    line = line.strip()
    if not line:
        return
    if line.startswith("#"):
        log.debug("firmware: %s", line)
        return
    key, sep, value = line.partition(":")
    metric = KEY_TO_METRIC.get(key)
    if not sep or metric is None:
        log.warning("Unrecognized serial line (protocol drift?): %r", line)
        return
    try:
        db.insert_reading(metric, float(value))
    except ValueError:
        log.warning("Non-numeric value in serial line: %r", line)


def send_command(command: str) -> bool:
    """Write one command line to the Arduino. Returns False if not connected."""
    if config.MOCK_HARDWARE:
        log.info("MOCK serial command: %s", command)
        return True
    with _port_lock:
        if _port is None:
            log.error("Cannot send %r: serial port not connected", command)
            return False
        try:
            _port.write((command.strip() + "\n").encode("ascii"))
            return True
        except Exception:
            log.exception("Serial write failed for %r", command)
            return False


def _read_loop() -> None:
    global _port
    import serial  # imported here so mock mode never needs pyserial's port

    while True:
        try:
            port = serial.Serial(config.SERIAL_PORT, config.SERIAL_BAUD, timeout=2)
        except Exception as exc:
            log.error(
                "SERIAL UNAVAILABLE: cannot open %s (%s). Arduino unplugged? "
                "Retrying in %ss. (Set MOCK_HARDWARE=1 to develop without it.)",
                config.SERIAL_PORT, exc, RECONNECT_DELAY_S,
            )
            time.sleep(RECONNECT_DELAY_S)
            continue

        log.info("Serial connected on %s @ %d baud", config.SERIAL_PORT, config.SERIAL_BAUD)
        with _port_lock:
            _port = port
        try:
            while True:
                raw = port.readline()  # b"" on timeout — loop keeps the port alive
                if raw:
                    handle_line(raw.decode("ascii", errors="replace"))
        except Exception:
            log.exception("SERIAL DROPPED on %s; reconnecting in %ss",
                          config.SERIAL_PORT, RECONNECT_DELAY_S)
        finally:
            with _port_lock:
                _port = None
            try:
                port.close()
            except Exception:
                pass
        time.sleep(RECONNECT_DELAY_S)


def _mock_loop() -> None:
    """Generate plausible sensor data on the firmware's 5s cadence."""
    log.warning("MOCK_HARDWARE=1: generating fake sensor data (no serial port)")
    t0 = time.time()
    while True:
        minutes = (time.time() - t0) / 60
        # Slow sinusoidal drift + jitter, so charts have visible shape.
        db.insert_reading("temp", round(21.5 + 1.5 * math.sin(minutes / 20) + random.uniform(-0.1, 0.1), 1))
        db.insert_reading("hum", round(45 + 6 * math.sin(minutes / 33 + 1) + random.uniform(-0.5, 0.5), 1))
        db.insert_reading("lux", max(0, round(300 + 250 * math.sin(minutes / 15) + random.uniform(-20, 20))))
        db.insert_reading("co2", max(420, round(650 + 180 * math.sin(minutes / 25 + 2) + random.uniform(-15, 15))))
        db.insert_reading("motion", 1 if random.random() < 0.15 else 0)
        time.sleep(5)


def start() -> threading.Thread:
    target = _mock_loop if config.MOCK_HARDWARE else _read_loop
    thread = threading.Thread(target=target, name="serial-reader", daemon=True)
    thread.start()
    return thread
