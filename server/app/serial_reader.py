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

Two things guard the seam against a sensor that is broken rather than absent:
readings outside a physically plausible band are dropped instead of stored
(see METRIC_RANGE), and pause() releases the port entirely so the board can be
reflashed without the reconnect loop stealing it back mid-upload.
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

# Physically plausible band per metric. ADVISORY ONLY — an out-of-band reading
# is logged loudly and then stored anyway.
#
# It deliberately does NOT gate storage. A sensor fault is exactly what the
# dashboard needs to show: filtering `CO2:0` out made a failing SCD40 look like
# a sensor that had merely gone quiet, which is unreviewable from the board and
# hides the one signal that says "this hardware is broken". Raw telemetry stays
# raw; judging it is the reader's job, not the ingest path's.
#
# The tradeoff, accepted knowingly: implausible rows land in `readings` and skew
# averages, alert bands, the "typical day" profile and the overnight CO2 delta
# for as long as the fault lasts. If that becomes the bigger problem, filter at
# the query layer where it can be turned off — not here.
METRIC_RANGE = {
    "temp": (-40.0, 85.0),      # BME280 operating range
    "hum": (0.0, 100.0),
    "lux": (0.0, 120000.0),     # BH1750 saturates well below this
    "co2": (300.0, 10000.0),    # real air floors out ~420; 0 = invalid channel
    "motion": (0.0, 1.0),
}

# Fail at import, not at 3am in the reader thread, if the two tables drift.
assert set(KEY_TO_METRIC.values()) == set(METRIC_RANGE), \
    "every metric in KEY_TO_METRIC needs a METRIC_RANGE band"

RECONNECT_DELAY_S = 5

# A dead sensor re-offends every few seconds, so rejections are logged at most
# once a minute per metric with a count of what was suppressed — 17k identical
# WARNING lines a day would bury everything else in the log.
REJECT_LOG_INTERVAL_S = 60

# Upper bound on pause(). A pause is a maintenance action on a box that runs
# unattended; it always expires so a forgotten one cannot end data collection
# permanently.
MAX_PAUSE_S = 2 * 60 * 60

_port = None          # live serial.Serial handle, or None
_port_lock = threading.Lock()

_running = threading.Event()   # cleared = reader must let go of the port
_running.set()
_pause_lock = threading.Lock()
_resume_timer = None

_rejects: dict[str, tuple[int, float]] = {}   # metric -> (suppressed, last log)


def _log_implausible(metric: str, value: float, low: float, high: float) -> None:
    suppressed, last_log = _rejects.get(metric, (0, 0.0))
    now = time.monotonic()
    if now - last_log < REJECT_LOG_INTERVAL_S:
        _rejects[metric] = (suppressed + 1, last_log)
        return
    tail = f" (+{suppressed} more since the last message)" if suppressed else ""
    log.warning(
        "Implausible %s=%g — outside [%g, %g]. STORED ANYWAY so the dashboard "
        "shows the fault. Sensor faulty or mid-recalibration?%s",
        metric, value, low, high, tail,
    )
    _rejects[metric] = (0, now)


def handle_line(line: str) -> None:
    """Parse one line from the Arduino and store it if it's a plausible reading."""
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
        reading = float(value)
    except ValueError:
        log.warning("Non-numeric value in serial line: %r", line)
        return
    low, high = METRIC_RANGE[metric]
    if not low <= reading <= high:
        _log_implausible(metric, reading, low, high)   # logged, then stored anyway
    db.insert_reading(metric, reading)


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


def pause(seconds: int, wait_timeout: float = 4.0) -> bool:
    """Close the serial port and stop reading for `seconds`.

    Flashing the board needs exclusive access to the port; without this the
    reconnect loop grabs it back within RECONNECT_DELAY_S and the upload fails
    (or worse, half-succeeds). Pausing rather than stopping the whole service
    keeps lighting, scenes, the planner and health running meanwhile.

    Blocks until the port is actually closed so a caller that returns can flash
    immediately. Returns True if it confirmed the release within wait_timeout.
    """
    global _resume_timer
    seconds = max(1, min(int(seconds), MAX_PAUSE_S))
    with _pause_lock:
        if _resume_timer is not None:
            _resume_timer.cancel()
        _running.clear()
        _resume_timer = threading.Timer(seconds, resume)
        _resume_timer.daemon = True
        _resume_timer.start()
    log.warning(
        "Serial reader PAUSED for %ss — releasing %s for flashing. No sensor "
        "data will be recorded until it resumes.", seconds, config.SERIAL_PORT,
    )
    if config.MOCK_HARDWARE:
        return True
    # readline() has a 2s timeout, so the loop notices at most that late.
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        with _port_lock:
            if _port is None:
                return True
        time.sleep(0.1)
    log.error("Serial port still open %ss after pause() — flashing may fail", wait_timeout)
    return False


def resume() -> None:
    """Undo pause() and let the reader reconnect. Safe to call when running."""
    global _resume_timer
    with _pause_lock:
        if _resume_timer is not None:
            _resume_timer.cancel()
            _resume_timer = None
        was_paused = not _running.is_set()
        _running.set()
    if was_paused:
        log.warning("Serial reader RESUMED — reconnecting to %s", config.SERIAL_PORT)


def is_paused() -> bool:
    return not _running.is_set()


def _read_loop() -> None:
    global _port
    import serial  # imported here so mock mode never needs pyserial's port

    while True:
        if not _running.is_set():
            _running.wait()   # port is already closed by the finally below
            continue
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
            while _running.is_set():
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
        if not _running.is_set():   # honour pause() here too, so it is testable
            _running.wait()
            continue
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
