"""REST API blueprint — the endpoint shape defined in CLAUDE.md."""

import logging
import re
import time

from flask import Blueprint, jsonify, request

from . import db, lighting, poller, scenes, serial_reader
from .mystrom import PlugError
from .wled import WledError

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


def _wled_state_or_none(device_id: int) -> dict | None:
    """Live read of a WLED zone's state — WLED zones aren't polled onto a
    schedule (there's no history chart for them yet), so this hits the zone
    directly on each /devices call."""
    zone = lighting.zones.get(device_id)
    if zone is None:
        return None
    try:
        return zone.state()
    except WledError as exc:
        log.warning("WLED state fetch failed for device %d: %s", device_id, exc)
        return None


def _attach_device_state(device: dict) -> dict:
    if device["type"] == "wifi_plug":
        device["power"] = db.latest_power(device["id"])
    elif device["type"] == "wled_zone":
        device["light"] = _wled_state_or_none(device["id"])
    return device


@api.get("/devices")
def devices():
    """Device list, each row including its last polled power sample (plugs)
    or live state (WLED zones) — one call refreshes every widget/card."""
    return jsonify([_attach_device_state(d) for d in db.list_devices()])


@api.get("/devices/<int:device_id>")
def device_detail(device_id: int):
    device = db.get_device(device_id)
    if device is None:
        return jsonify({"error": "no such device"}), 404
    return jsonify(_attach_device_state(device))


@api.post("/devices/<int:device_id>/toggle")
def device_toggle(device_id: int):
    """Flip the plug relay. A locked plug refuses ANY toggle (on or off)
    with 423 — it must be unlocked first via /lock, which is the only place
    the "type UNLOCK" confirmation lives."""
    plug = poller.plugs.get(device_id)
    if plug is None:
        return jsonify({"error": "no such wifi plug"}), 404

    device = db.get_device(device_id)
    if device and device.get("locked"):
        return jsonify({"error": "plug is locked — unlock it first", "locked": True}), 423

    try:
        relay_on = plug.toggle()
    except PlugError as exc:
        log.error("Toggle failed: %s", exc)
        return jsonify({"error": str(exc)}), 502
    # Record the new state immediately so the UI doesn't wait a poll cycle.
    db.insert_power_reading(device_id, None, relay_on)
    return jsonify({"relay_on": relay_on})


@api.post("/devices/<int:device_id>/lock")
def device_lock(device_id: int):
    """Arm/disarm the power-off lock on a plug. Body: {"locked": bool}.
    Arming needs no ceremony. Disarming an already-locked plug requires
    {"confirm": "unlock"} (case-insensitive) — that typed confirmation is
    what actually clears the lock; /toggle itself never accepts a bypass."""
    device = db.get_device(device_id)
    if device is None or device["type"] != "wifi_plug":
        return jsonify({"error": "no such wifi plug"}), 404
    body = request.get_json(silent=True) or {}
    locked = body.get("locked")
    if not isinstance(locked, bool):
        return jsonify({"error": "locked must be a boolean"}), 400
    if device.get("locked") and not locked:
        confirm = body.get("confirm", "")
        if not isinstance(confirm, str) or confirm.strip().lower() != "unlock":
            return jsonify({"error": "type UNLOCK to confirm unlocking this plug"}), 423
    db.set_device_locked(device_id, locked)
    log.info("Device %d lock set to %s", device_id, locked)
    return jsonify({"locked": locked})


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


@api.post("/devices/<int:device_id>/state")
def device_state(device_id: int):
    """Set a WLED zone's brightness/color/effect/on-off. Body: any subset of
    {"on": bool, "brightness": 0-255, "color": [r, g, b], "effect": int}.
    Only meaningful in 'manual' mode — in 'auto' mode the lighting job will
    overwrite brightness/on on its next tick."""
    device = db.get_device(device_id)
    if device is None or device["type"] != "wled_zone":
        return jsonify({"error": "no such WLED zone"}), 404
    zone = lighting.zones.get(device_id)
    if zone is None:
        return jsonify({"error": "zone not configured"}), 404

    def _num(v):  # bool is an int subclass — don't let true/false pass as 1/0
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    body = request.get_json(silent=True) or {}
    brightness = body.get("brightness")
    if brightness is not None and not (_num(brightness) and 0 <= brightness <= 255):
        return jsonify({"error": "brightness must be 0-255"}), 400
    color = body.get("color")
    if color is not None and (not isinstance(color, list) or len(color) != 3
                               or not all(_num(c) and 0 <= c <= 255 for c in color)):
        return jsonify({"error": "color must be [r, g, b] with each 0-255"}), 400
    on = body.get("on")
    if on is not None and not isinstance(on, bool):
        return jsonify({"error": "on must be a boolean"}), 400
    effect = body.get("effect")
    if effect is not None and (not isinstance(effect, int) or isinstance(effect, bool)):
        return jsonify({"error": "effect must be an integer"}), 400

    try:
        state = zone.set_state(
            on=on,
            brightness=int(brightness) if brightness is not None else None,
            color=[int(c) for c in color] if color is not None else None,
            effect=effect,
        )
    except WledError as exc:
        log.error("WLED set_state failed for device %d: %s", device_id, exc)
        return jsonify({"error": str(exc)}), 502
    return jsonify(state)


@api.post("/devices/<int:device_id>/mode")
def device_mode(device_id: int):
    """Set a WLED zone's mode. Body: {"mode": "manual" | "auto"}. In 'auto'
    the lighting job (see app/lighting.py) drives brightness from the
    latest BH1750 lux reading instead of the dashboard."""
    device = db.get_device(device_id)
    if device is None or device["type"] != "wled_zone":
        return jsonify({"error": "no such WLED zone"}), 404
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode not in ("manual", "auto"):
        return jsonify({"error": "mode must be 'manual' or 'auto'"}), 400
    db.set_device_mode(device_id, mode)
    log.info("Device %d mode set to %s", device_id, mode)
    return jsonify({"mode": mode})


@api.get("/scenes")
def scenes_list():
    """House modes and their per-device target states (see app/scenes.py)."""
    return jsonify(db.list_scenes())


@api.post("/scenes/<name>/activate")
def scene_activate(name: str):
    """Activate a scene by name (case-insensitive). Body (optional):
    {"wake_time": "HH:MM"} — only when activating Sleeping; schedules an
    automatic switch back to Day at that local time (a scene change only —
    NOT an alarm). Blank/absent means Sleeping holds until changed manually."""
    body = request.get_json(silent=True) or {}
    wake_time = body.get("wake_time")
    if isinstance(wake_time, str):
        wake_time = wake_time.strip() or None  # blank picker == no wake time
    elif wake_time is not None:
        return jsonify({"error": "wake_time must be an HH:MM string"}), 400
    try:
        result = scenes.activate(name, wake_time)
    except scenes.SceneError as exc:
        return jsonify({"error": str(exc)}), 400
    if result is None:
        return jsonify({"error": f"no such scene {name!r}"}), 404
    return jsonify(result)


@api.get("/scenes/active")
def scene_active():
    """Current scene + activation time + pending wake time (if any).
    Defaults to Day when no scene has ever been activated."""
    return jsonify(scenes.active_info())


@api.get("/scenes/last-summary")
def scene_last_summary():
    """Most recent Sleeping->Day overnight summary, or {"summary": null}
    before the first one exists."""
    return jsonify({"summary": db.get_setting("last_sleep_summary")})


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
