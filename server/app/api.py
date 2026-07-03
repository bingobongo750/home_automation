"""REST API blueprint — the endpoint shape defined in CLAUDE.md."""

import logging
import re
import time

from flask import Blueprint, jsonify, request

from . import db, poller, serial_reader
from .mystrom import PlugError

log = logging.getLogger("api")

api = Blueprint("api", __name__, url_prefix="/api")

VALID_METRICS = {"temp", "hum", "lux", "co2", "motion"}

_RANGE_RE = re.compile(r"^(\d+)([mhd])$")
_RANGE_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def parse_range(default: str = "24h") -> float:
    """'30m' | '24h' | '7d' -> unix timestamp for the start of the window."""
    raw = request.args.get("range", default)
    m = _RANGE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"bad range {raw!r}, expected e.g. 30m, 24h, 7d")
    return time.time() - int(m.group(1)) * _RANGE_SECONDS[m.group(2)]


@api.get("/sensors/latest")
def sensors_latest():
    return jsonify(db.latest_readings())


@api.get("/settings/thresholds")
def get_thresholds():
    return jsonify(db.get_thresholds())


@api.put("/settings/thresholds")
def put_thresholds():
    """Replace alert thresholds. Body: {"temp": {"min": 17, "max": 26}, ...}
    for keys temp/hum/lux/co2/power; null (or a missing bound) disables it."""
    body = request.get_json(silent=True) or {}
    clean = {}
    for key in db.DEFAULT_THRESHOLDS:
        entry = body.get(key)
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            return jsonify({"error": f"{key} must be an object with min/max"}), 400
        bounds = {}
        for bound in ("min", "max"):
            value = entry.get(bound)
            if value is None or value == "":
                bounds[bound] = None
            else:
                try:
                    bounds[bound] = float(value)
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key}.{bound} must be a number or null"}), 400
        if bounds["min"] is not None and bounds["max"] is not None and bounds["min"] >= bounds["max"]:
            return jsonify({"error": f"{key}: min must be below max"}), 400
        clean[key] = bounds
    db.set_thresholds(clean)
    log.info("Alert thresholds updated: %s", clean)
    return jsonify(clean)


@api.get("/sensors/history")
def sensors_history():
    metric = request.args.get("metric", "")
    if metric not in VALID_METRICS:
        return jsonify({"error": f"unknown metric {metric!r}, expected one of {sorted(VALID_METRICS)}"}), 400
    try:
        since = parse_range()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"metric": metric, "points": db.metric_history(metric, since)})


@api.get("/sensors/stats")
def sensors_stats():
    """24h min/max/avg + 7d avg for one metric (expanded-widget view)."""
    metric = request.args.get("metric", "")
    if metric not in VALID_METRICS:
        return jsonify({"error": f"unknown metric {metric!r}, expected one of {sorted(VALID_METRICS)}"}), 400
    return jsonify(db.metric_stats(metric))


@api.get("/sensors/profile")
def sensors_profile():
    """'Typical day' curve: 7-day average per time-of-day bucket. Optional
    ?bucket=<minutes> (default 30, clamped 5-120) — the dashboard uses finer
    buckets for short-range charts."""
    metric = request.args.get("metric", "")
    if metric not in VALID_METRICS:
        return jsonify({"error": f"unknown metric {metric!r}, expected one of {sorted(VALID_METRICS)}"}), 400
    try:
        bucket = min(max(int(request.args.get("bucket", "30")), 5), 120)
    except ValueError:
        return jsonify({"error": "bucket must be an integer number of minutes"}), 400
    return jsonify({
        "metric": metric,
        "days": 7,
        "bucket_minutes": bucket,
        "points": db.metric_daily_profile(metric, bucket_minutes=bucket),
    })


@api.get("/motion/events")
def motion_events():
    """Recent motion detections for the activity log (newest first)."""
    try:
        since = parse_range()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"events": db.motion_events(since), "count": db.motion_count(since)})


@api.get("/devices")
def devices():
    """Device list, each row including its last polled power sample — one
    call refreshes every plug widget on the dashboard."""
    out = []
    for device in db.list_devices():
        device["power"] = db.latest_power(device["id"])
        out.append(device)
    return jsonify(out)


@api.get("/devices/<int:device_id>")
def device_detail(device_id: int):
    device = db.get_device(device_id)
    if device is None:
        return jsonify({"error": "no such device"}), 404
    device["power"] = db.latest_power(device_id)  # last polled state + watts
    return jsonify(device)


@api.post("/devices/<int:device_id>/toggle")
def device_toggle(device_id: int):
    plug = poller.plugs.get(device_id)
    if plug is None:
        return jsonify({"error": "no such wifi plug"}), 404
    try:
        relay_on = plug.toggle()
    except PlugError as exc:
        log.error("Toggle failed: %s", exc)
        return jsonify({"error": str(exc)}), 502
    # Record the new state immediately so the UI doesn't wait a poll cycle.
    db.insert_power_reading(device_id, None, relay_on)
    return jsonify({"relay_on": relay_on})


@api.get("/devices/<int:device_id>/power/stats")
def device_power_stats(device_id: int):
    if db.get_device(device_id) is None:
        return jsonify({"error": "no such device"}), 404
    return jsonify(db.power_stats(device_id))


@api.get("/devices/<int:device_id>/power/history")
def device_power_history(device_id: int):
    if db.get_device(device_id) is None:
        return jsonify({"error": "no such device"}), 404
    try:
        since = parse_range()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"device_id": device_id, "points": db.power_history(device_id, since)})


@api.post("/arduino/command")
def arduino_command():
    """Send a raw protocol command line to the Arduino (RELAY1:ON, DIM1:180,
    MODE:aqi, COLOR:r,g,b). Exists so the future Lighting tab and any manual
    testing have a path to the wired lane."""
    command = (request.get_json(silent=True) or {}).get("command", "").strip()
    if not command or ":" not in command:
        return jsonify({"error": "body must be {\"command\": \"KEY:VALUE\"}"}), 400
    if not serial_reader.send_command(command):
        return jsonify({"error": "serial port not connected"}), 502
    return jsonify({"sent": command})
