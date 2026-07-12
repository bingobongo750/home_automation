"""House modes (scenes): Sleeping / Day / Away.

A scene is a named, manually-triggered state that sets several devices at
once — the myStrom plug(s) via poller.plugs and the WLED zones via
lighting.zones. Definitions live in the `scenes` table (seeded from
db.SCENE_SEEDS, editable there); states are keyed by the group keys
"all_plugs"/"all_zones" (every device of that type) and/or device names
(overriding the group for that device) — see GROUP_KEYS below. The active
scene persists in the settings table so it survives backend restarts.

While any scene other than "Day" is active, the auto-lighting job
(app/lighting.py) is suppressed so the scene's explicit values win. Zones'
`mode` columns are never rewritten by a scene — returning to "Day" lifts the
suppression, and any zone still set to 'auto' resumes lux-driven brightness
on the next tick (activation pokes the job so that tick happens immediately).

Wake time: activating Sleeping may carry an optional "HH:MM" wake time. A
plain threading.Timer (in-process, no job-queue dependency) then switches the
scene to Day at that time. It ONLY switches the scene — it is not an alarm
and never notifies, sounds, or wakes anyone. Any scene activation cancels the
pending timer, so a manual change before the wake time always wins; a
generation counter makes an already-running stale timer a no-op. The pending
wake is stored with the active scene, so init() re-arms it after a restart
(an overdue wake fires immediately).

Morning summary: every Sleeping -> Day transition (scheduled or manual)
computes overnight stats from the readings table over the Sleeping window —
temp/hum min/max/avg, CO2 start vs end (flagged if it climbed significantly),
and motion events — plus a planner snapshot (today's events, overdue/
high-priority tasks, see app/planner.py) — and stores them in settings for
GET /api/scenes/last-summary.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta

from . import db, health, lighting, planner, poller
from .mystrom import PlugError
from .wled import WledError

log = logging.getLogger("scenes")

# The neutral scene: normal operation, auto lighting enabled. Also what the
# backend assumes when no scene has ever been activated.
DEFAULT_SCENE = "Day"

# CO2 rise (ppm) across the sleep window that flags "climbed significantly"
# in the morning summary — roughly one "ventilate soon" step.
CO2_RISE_FLAG_PPM = 200

_WAKE_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Reentrant so a firing wake timer can hold the lock across its staleness
# check AND the activate() call it makes — no gap for a concurrent manual
# activation to slip into.
_lock = threading.RLock()
_wake_timer: threading.Timer | None = None
_wake_generation = 0  # bumped on every arm/cancel; stale timers see a mismatch


class SceneError(Exception):
    """Bad activation request (unknown wake time format, etc.)."""


def next_wake_at(wake_time: str, now: float | None = None) -> float:
    """Unix timestamp of the first occurrence of local time "HH:MM" strictly
    after `now` — today if still ahead, otherwise tomorrow."""
    now_dt = datetime.fromtimestamp(now if now is not None else time.time())
    hour, minute = (int(part) for part in wake_time.split(":"))
    target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_dt:
        target += timedelta(days=1)
    return target.timestamp()


def active_info() -> dict:
    """Current scene for GET /api/scenes/active — defaults to Day (with no
    activation timestamp) when nothing was ever activated."""
    active = db.get_active_scene()
    if active is None:
        return {"name": DEFAULT_SCENE, "activated_at": None,
                "wake_time": None, "wake_at": None}
    return active


def activate(name: str, wake_time: str | None = None, *,
             source: str = "manual") -> dict | None:
    """Activate a scene: persist it, apply its device targets, and manage the
    wake timer. Returns the API response dict, or None for an unknown scene.
    Raises SceneError on a bad wake_time.

    Held under _lock end to end (including device I/O, worst case a few
    seconds of HTTP timeouts) so concurrent activations and a firing wake
    timer serialize instead of interleaving half-applied states.
    """
    scene = db.get_scene(name)
    if scene is None:
        return None

    if wake_time is not None:
        if scene["name"] != "Sleeping":
            raise SceneError("wake_time is only accepted when activating Sleeping")
        if not _WAKE_TIME_RE.match(wake_time):
            raise SceneError(f"wake_time must be HH:MM (24h), got {wake_time!r}")

    with _lock:
        _cancel_wake_locked()
        prev = db.get_active_scene()
        now = time.time()

        summary = None
        if prev and prev["name"] == "Sleeping" and scene["name"] == "Day":
            summary = _compute_sleep_summary(prev["activated_at"], now)
            db.set_setting("last_sleep_summary", summary)
            log.info("Overnight summary stored for %.1fh Sleeping window",
                     (now - prev["activated_at"]) / 3600)

        # Re-activating the current scene (e.g. changing the wake time mid-
        # night) keeps the original activation time — the summary window
        # should still cover the whole night.
        activated_at = (prev["activated_at"]
                        if prev and prev["name"] == scene["name"] and prev["activated_at"]
                        else now)

        wake_at = None
        if wake_time is not None:
            wake_at = next_wake_at(wake_time, now)
            _arm_wake_locked(wake_at)

        db.set_active_scene(scene["name"], activated_at, wake_time, wake_at)
        results = _apply_states(scene["states"])

    lighting.poke()  # suppression changed either way — let the job react now
    log.info("Scene '%s' activated (%s)%s", scene["name"], source,
             f", wake to Day at {wake_time}" if wake_time else "")
    return {
        "active": {"name": scene["name"], "activated_at": activated_at,
                   "wake_time": wake_time, "wake_at": wake_at},
        "devices": results,
        "summary_generated": summary is not None,
    }


def init() -> None:
    """Called once at startup (after poller/lighting built their device
    clients): restore a pending Sleeping->Day wake from the persisted active
    scene. An overdue wake — the time passed while the backend was down —
    fires immediately, synchronously, so the house isn't stuck in Sleeping."""
    active = db.get_active_scene()
    if not active or active["name"] != "Sleeping" or not active.get("wake_at"):
        return
    if active["wake_at"] <= time.time():
        log.info("Wake time %s passed while the backend was down — switching to Day now",
                 active["wake_time"])
        activate(DEFAULT_SCENE, source="overdue wake after restart")
    else:
        with _lock:
            _arm_wake_locked(active["wake_at"])
        log.info("Re-armed pending wake: Sleeping -> Day at %s", active["wake_time"])


# ------------------------------------------------------------- wake timer

def _arm_wake_locked(wake_at: float) -> None:
    global _wake_timer, _wake_generation
    _wake_generation += 1
    _wake_timer = threading.Timer(max(wake_at - time.time(), 0.0),
                                  _fire_wake, args=(_wake_generation,))
    _wake_timer.daemon = True
    _wake_timer.name = "scene-wake"
    _wake_timer.start()


def _cancel_wake_locked() -> None:
    global _wake_timer, _wake_generation
    _wake_generation += 1  # a timer that already started firing becomes stale
    if _wake_timer is not None:
        _wake_timer.cancel()
        _wake_timer = None


def _fire_wake(generation: int) -> None:
    """Timer callback: switch Sleeping -> Day, unless this timer was
    superseded by a manual scene change after it was armed."""
    with _lock:
        if generation != _wake_generation:
            log.info("Stale wake timer ignored (scene changed before it fired)")
            return
        active = db.get_active_scene()
        if not active or active["name"] != "Sleeping":
            log.info("Wake timer fired but scene is no longer Sleeping — ignored")
            return
        log.info("Wake time reached — switching Sleeping -> Day (scene change only, not an alarm)")
        try:
            activate(DEFAULT_SCENE, source="wake schedule")
        except Exception:
            log.exception("Scheduled Sleeping -> Day transition failed")


# --------------------------------------------------------- device application

# Scene state group keys: apply a target to every device of that type —
# present and future, so "all plugs off" never depends on a name list.
GROUP_KEYS = {"all_plugs": "wifi_plug", "all_zones": "wled_zone"}


def _resolve_targets(states: dict, devices: dict) -> tuple[dict, list[dict]]:
    """Expand a scene's states into one merged target per actual device.
    Group keys ("all_plugs"/"all_zones") seed every device of that type;
    a per-device-name entry then overrides the group's fields for that
    device. Unknown device names are reported, not fatal."""
    targets: dict[str, dict] = {}
    results: list[dict] = []
    for key, dtype in GROUP_KEYS.items():
        target = states.get(key)
        if target:
            for device in devices.values():
                if device["type"] == dtype:
                    targets[device["name"]] = dict(target)
    for name, target in states.items():
        if name in GROUP_KEYS:
            continue
        if name not in devices:
            log.warning("Scene targets unknown device %r — skipped", name)
            results.append({"device": name, "ok": False, "error": "unknown device"})
            continue
        merged = targets.setdefault(name, {})
        merged.update(target)
    return targets, results


def _apply_states(states: dict) -> list[dict]:
    """Push each device's target. One unreachable device never blocks the
    rest — failures are logged, reported per-device, and the scene still
    counts as active.

    Holds lighting.push_lock so an auto-lighting tick already in flight
    finishes (or is dropped by its own suppression re-check) before the
    scene's values go out — the scene is always the last writer."""
    devices = {d["name"]: d for d in db.list_devices()}
    targets, results = _resolve_targets(states, devices)
    with lighting.push_lock:
        for device_name, target in targets.items():
            device = devices[device_name]
            try:
                if device["type"] == "wifi_plug":
                    results.append(_apply_plug(device, target))
                elif device["type"] == "wled_zone":
                    results.append(_apply_zone(device, target))
                else:
                    results.append({"device": device_name, "ok": False,
                                    "error": f"unsupported device type {device['type']}"})
            except (PlugError, WledError) as exc:
                log.error("Scene could not reach %s: %s", device_name, exc)
                results.append({"device": device_name, "ok": False, "error": str(exc)})
    return results


def _apply_plug(device: dict, target: dict) -> dict:
    on = target.get("on")
    if on is None:
        return {"device": device["name"], "ok": True, "skipped": "no target fields"}
    if device.get("locked"):
        # The lock exists to stop accidental switching — a scene doesn't get
        # to bypass what the dashboard's toggle can't.
        log.warning("Scene left locked plug %s untouched (wanted on=%s)", device["name"], on)
        return {"device": device["name"], "ok": False, "skipped": "locked"}
    plug = poller.plugs.get(device["id"])
    if plug is None:
        return {"device": device["name"], "ok": False, "error": "plug not configured"}
    plug.set_state(bool(on))
    # Record the new state immediately so the UI doesn't wait a poll cycle
    # (same as api.device_toggle).
    db.insert_power_reading(device["id"], None, bool(on))
    return {"device": device["name"], "ok": True}


def _apply_zone(device: dict, target: dict) -> dict:
    zone = lighting.zones.get(device["id"])
    if zone is None:
        return {"device": device["name"], "ok": False, "error": "zone not configured"}
    zone.set_state(
        on=target.get("on"),
        brightness=target.get("brightness"),
        color=target.get("color"),
        effect=target.get("effect"),
    )
    return {"device": device["name"], "ok": True}


# ------------------------------------------------------------ morning summary

def _compute_sleep_summary(since: float, until: float) -> dict:
    """Overnight stats from the existing readings table, plus the planner's
    look at the day being woken into — computed once at the Sleeping -> Day
    transition, stored as settings.last_sleep_summary."""
    co2_start, co2_end = db.metric_window_endpoints("co2", since, until)
    co2_delta = round(co2_end - co2_start) if co2_start is not None and co2_end is not None else None
    co2_avg = db.metric_window_stats("co2", since, until)["avg"]
    return {
        "from": since,
        "to": until,
        "temp": db.metric_window_stats("temp", since, until),
        "hum": db.metric_window_stats("hum", since, until),
        "co2": {
            # avg is the dashboard's headline number (a signed delta up front
            # read like a negative CO2 level); start/end/delta stay for the
            # trend line and the ventilate flag
            "avg": round(co2_avg) if co2_avg is not None else None,
            "start": round(co2_start) if co2_start is not None else None,
            "end": round(co2_end) if co2_end is not None else None,
            "delta": co2_delta,
            "rose_significantly": co2_delta is not None and co2_delta >= CO2_RISE_FLAG_PPM,
        },
        "motion": {
            "count": db.motion_count(since, until),
            "events": [e["ts"] for e in db.motion_events(since, limit=30, until=until)],
        },
        # today's events + overdue/high-priority tasks — same summary, one
        # more section, so the morning card stays a single report
        "planner": planner.morning_snapshot(until),
        # last night's recovery + sleep scores (None before any health data,
        # and on summaries stored before the health module existed)
        "health": health.morning_snapshot(until),
    }
