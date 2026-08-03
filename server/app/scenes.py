"""House modes (scenes): Sleeping / Home / Away.

A scene is a named, manually-triggered state that sets several devices at
once — the myStrom plug(s) via poller.plugs and the bulb zones via
lighting.zones. Definitions live in the `scenes` table (seeded from
db.SCENE_SEEDS, editable there); states are keyed by the group keys
"all_plugs"/"all_zones" (every device of that type) and/or device names
(overriding the group for that device) — see GROUP_KEYS below. The active
scene persists in the settings table so it survives backend restarts.

While any scene other than "Home" is active, the auto-lighting job
(app/lighting.py) is suppressed so the scene's explicit values win. Zones'
`mode` columns are never rewritten by a scene — returning to "Home" lifts the
suppression, and any zone still set to 'auto' resumes lux-driven brightness
on the next tick (activation pokes the job so that tick happens immediately).

Wake time: activating Sleeping may carry an optional "HH:MM" wake time. A
plain threading.Timer (in-process, no job-queue dependency) then switches the
scene to Home at that time. It ONLY switches the scene — it is not an alarm
and never notifies, sounds, or wakes anyone. Any scene activation cancels the
pending timer, so a manual change before the wake time always wins; a
generation counter makes an already-running stale timer a no-op. The pending
wake is stored with the active scene, so init() re-arms it after a restart
(an overdue wake fires immediately).

Nightly schedule: a stored sleep window (db.get_sleep_schedule — default
00:00 to 09:30, editable from the dashboard's settings dialog) activates
Sleeping every night and hands back to Home in the morning, reusing the wake
machinery above so the morning summary works exactly as it does manually. Its
bedtime timer is separate from the wake timer, since a manual scene change
must beat tonight's pending wake without cancelling tomorrow's bedtime. Like
the wake timer it only switches scenes — it is not an alarm.

Morning summary: every Sleeping -> Home transition (scheduled or manual)
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

from . import config, db, health, lighting, planner, poller
from .mystrom import PlugError
from .shelly_bulb import BulbError

log = logging.getLogger("scenes")

# The neutral scene: normal operation, auto lighting enabled. Also what the
# backend assumes when no scene has ever been activated.
DEFAULT_SCENE = "Home"

# CO2 rise (ppm) across the sleep window that flags "climbed significantly"
# in the morning summary — roughly one "ventilate soon" step.
CO2_RISE_FLAG_PPM = 200

WAKE_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Reentrant so a firing wake timer can hold the lock across its staleness
# check AND the activate() call it makes — no gap for a concurrent manual
# activation to slip into.
_lock = threading.RLock()
_wake_timer: threading.Timer | None = None
_wake_generation = 0  # bumped on every arm/cancel; stale timers see a mismatch

# The nightly schedule's bedtime timer is deliberately SEPARATE from the wake
# timer above: activate() cancels the wake timer (a manual scene change must
# beat a pending wake), but the recurring schedule has to survive that and
# still come round the next night.
_bedtime_timer: threading.Timer | None = None
_bedtime_generation = 0


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
    """Current scene for GET /api/scenes/active — defaults to Home (with no
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
        if not WAKE_TIME_RE.match(wake_time):
            raise SceneError(f"wake_time must be HH:MM (24h), got {wake_time!r}")

    with _lock:
        _cancel_wake_locked()
        prev = db.get_active_scene()
        now = time.time()

        summary = None
        if prev and prev["name"] == "Sleeping" and scene["name"] == "Home":
            summary = _compute_sleep_summary(prev["activated_at"], now)
            db.set_setting("last_sleep_summary", summary)
            # ...and keep it, so nights can be reviewed later. settings only
            # ever holds the most recent one.
            db.save_night_summary(prev["activated_at"], now, summary)
            log.info("Overnight summary stored for %.1fh Sleeping window",
                     (now - prev["activated_at"]) / 3600)

        # The away summary belongs HERE, not in presence.arrived(), because it
        # has to fire on EVERY way out of Away — not just a phone arrival. The
        # case that most needs it is the one where the arrival Shortcut failed
        # (Tailscale asleep, iOS disabled the automation): you get home to an
        # Away house and tap Home on the dashboard, and that is exactly when
        # you want to know what happened while you were out.
        away_summary = None
        if (prev and prev["name"] == "Away" and prev.get("activated_at")
                and scene["name"] in ("Home", "Sleeping")):
            # Trim the tail whatever the trigger. A phone arrival is detected
            # late; a manual tap is later still, since you walked in, found the
            # house dark and crossed the room to the dashboard. Either way the
            # last stretch of motion, CO2 and lux is you.
            until = now - config.PRESENCE_ARRIVAL_TRIM_S
            if until - prev["activated_at"] >= config.PRESENCE_SUMMARY_MIN_S:
                away_summary = _compute_away_summary(prev["activated_at"], until)
                db.set_setting("last_away_summary", away_summary)
                log.info("Away summary stored for a %.0f min absence (disturbed=%s)",
                         (now - prev["activated_at"]) / 60, away_summary["disturbed"])
            else:
                log.info("Absence too short for a summary (%.0f min)",
                         max(until - prev["activated_at"], 0) / 60)

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
             f", wake to Home at {wake_time}" if wake_time else "")
    return {
        "active": {"name": scene["name"], "activated_at": activated_at,
                   "wake_time": wake_time, "wake_at": wake_at},
        "devices": results,
        "summary_generated": summary is not None,
        "away_summary_generated": away_summary is not None,
    }


def init() -> None:
    """Called once at startup (after poller/lighting built their device
    clients): arm the nightly schedule, and restore a pending Sleeping->Home
    wake from the persisted active scene. An overdue wake — the time passed
    while the backend was down — fires immediately, synchronously, so the
    house isn't stuck in Sleeping."""
    reschedule_bedtime()
    active = db.get_active_scene()
    if not active or active["name"] != "Sleeping" or not active.get("wake_at"):
        return
    if active["wake_at"] <= time.time():
        log.info("Wake time %s passed while the backend was down — switching to Home now",
                 active["wake_time"])
        activate(DEFAULT_SCENE, source="overdue wake after restart")
    else:
        with _lock:
            _arm_wake_locked(active["wake_at"])
        log.info("Re-armed pending wake: Sleeping -> Home at %s", active["wake_time"])


# --------------------------------------------------------- nightly schedule

def reschedule_bedtime() -> None:
    """(Re)arm the nightly Sleeping activation from the stored schedule. Safe
    to call any time — at startup, and whenever the schedule is edited.

    A restart mid-window deliberately does NOT back-fill: it arms the next
    bedtime and leaves the current scene alone, so coming back up at 02:00
    never overrides a house someone deliberately put in Away.
    """
    schedule = db.get_sleep_schedule()
    with _lock:
        _cancel_bedtime_locked()
        if not schedule.get("enabled"):
            log.info("Nightly sleep schedule is off")
            return
        sleep_time = schedule["sleep_time"]
        if not WAKE_TIME_RE.match(sleep_time):
            log.error("Nightly sleep schedule has a bad sleep_time %r — not armed", sleep_time)
            return
        at = next_wake_at(sleep_time, time.time())
        _arm_bedtime_locked(at)
    log.info("Nightly schedule armed: Sleeping at %s, back to Home at %s",
             sleep_time, schedule["wake_time"])


def _arm_bedtime_locked(at: float) -> None:
    global _bedtime_timer, _bedtime_generation
    _bedtime_generation += 1
    _bedtime_timer = threading.Timer(max(at - time.time(), 0.0),
                                     _fire_bedtime, args=(_bedtime_generation,))
    _bedtime_timer.daemon = True
    _bedtime_timer.name = "scene-bedtime"
    _bedtime_timer.start()


def _cancel_bedtime_locked() -> None:
    global _bedtime_timer, _bedtime_generation
    _bedtime_generation += 1
    if _bedtime_timer is not None:
        _bedtime_timer.cancel()
        _bedtime_timer = None


def _fire_bedtime(generation: int) -> None:
    """Timer callback: activate Sleeping for the night, carrying the
    schedule's wake time so the existing Sleeping->Home machinery (and its
    morning summary) handles the other end. Re-arms itself for tomorrow."""
    try:
        with _lock:
            if generation != _bedtime_generation:
                log.info("Stale bedtime timer ignored (schedule changed before it fired)")
                return
            schedule = db.get_sleep_schedule()
            if not schedule.get("enabled"):
                return
            # Away is the strongest state: nothing automatic may override it.
            # Without this the nightly timer puts an empty flat into Sleeping,
            # and the morning wake then switches it to Home — lights and plugs
            # on in a house nobody is in, the exact opposite of the intent.
            # Same principle as init(), which refuses to back-fill a window it
            # slept through rather than overriding a deliberate Away.
            active = db.get_active_scene()
            if active and active["name"] == "Away":
                log.info("Nightly schedule: staying in Away (house is empty) — "
                         "not switching to Sleeping")
                return
            wake_time = schedule["wake_time"]
            if not WAKE_TIME_RE.match(wake_time):
                log.error("Nightly sleep schedule has a bad wake_time %r — "
                          "activating Sleeping with no wake", wake_time)
                wake_time = None
            log.info("Nightly schedule: switching to Sleeping (scene change only, not an alarm)")
            try:
                activate("Sleeping", wake_time, source="nightly schedule")
            except Exception:
                log.exception("Scheduled Home -> Sleeping transition failed")
    finally:
        # Always come round again, even if tonight's activation blew up —
        # one bad night must not silently end the recurring schedule.
        reschedule_bedtime()


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
    """Timer callback: switch Sleeping -> Home, unless this timer was
    superseded by a manual scene change after it was armed."""
    with _lock:
        if generation != _wake_generation:
            log.info("Stale wake timer ignored (scene changed before it fired)")
            return
        active = db.get_active_scene()
        if not active or active["name"] != "Sleeping":
            log.info("Wake timer fired but scene is no longer Sleeping — ignored")
            return
        log.info("Wake time reached — switching Sleeping -> Home (scene change only, not an alarm)")
        try:
            activate(DEFAULT_SCENE, source="wake schedule")
        except Exception:
            log.exception("Scheduled Sleeping -> Home transition failed")


# --------------------------------------------------------- device application

# Scene state group keys: apply a target to every device of that type —
# present and future, so "all plugs off" never depends on a name list.
GROUP_KEYS = {"all_plugs": "wifi_plug", "all_zones": "bulb_zone"}


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
                elif device["type"] == "bulb_zone":
                    results.append(_apply_zone(device, target))
                else:
                    results.append({"device": device_name, "ok": False,
                                    "error": f"unsupported device type {device['type']}"})
            except (PlugError, BulbError) as exc:
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
    # A scene may set ambient white (`ct`) or a custom colour (`rgb`), never
    # both — the bulb lights one channel at a time. `ct` wins if a hand-edited
    # row carries both, since ambient is the mostly-used mode.
    ct = target.get("ct")
    color = target.get("color")
    try:
        zone.set_state(
            on=target.get("on"),
            brightness=target.get("brightness"),
            color=None if ct is not None else color,
            ct=ct,
        )
    except ValueError as exc:   # malformed hand-edited scene row
        return {"device": device["name"], "ok": False, "error": str(exc)}
    return {"device": device["name"], "ok": True}


# ------------------------------------------------------------- away summary

def cluster_events(timestamps: list, gap_s: float) -> list[dict]:
    """Collapse a stream of detections into events.

    A PIR does not produce "a detection" — it produces a reading every cycle
    for as long as it keeps seeing movement, so one person crossing the room is
    dozens of rows. Counting rows would report "47 disturbances" for a single
    event and bury the only distinction that matters: one disturbance or
    several separate ones.

    Detections less than gap_s apart are one event; a longer gap starts a new
    one. Returns [{"start", "end", "samples"}] in chronological order.
    """
    events: list[dict] = []
    for ts in sorted(timestamps):
        if events and ts - events[-1]["end"] < gap_s:
            events[-1]["end"] = ts
            events[-1]["samples"] += 1
        else:
            events.append({"start": ts, "end": ts, "samples": 1})
    return events


def _compute_away_summary(since: float, until: float) -> dict:
    """What happened while the house was empty — computed once at the
    Away -> (Home|Sleeping) transition, stored as settings.last_away_summary.

    Deliberately different content from the overnight summary: that one is
    about sleep quality, this one is about whether anything happened in a room
    nobody was in. `until` has already been trimmed by the caller.
    """
    co2_start, co2_end = db.metric_window_endpoints("co2", since, until)
    co2_delta = (round(co2_end - co2_start)
                 if co2_start is not None and co2_end is not None else None)
    co2_stats = db.metric_window_stats("co2", since, until)
    co2_rose = co2_delta is not None and co2_delta >= CO2_RISE_FLAG_PPM

    motion_ts = [e["ts"] for e in db.motion_events(since, limit=2000, until=until)]
    motion_events = cluster_events(motion_ts, config.DISTURBANCE_COOLDOWN_S)

    plugs = []
    for device in db.list_devices():
        if device["type"] != "wifi_plug":
            continue
        stats = db.plug_window_summary(device["id"], since, until)
        stats["name"] = device["name"]
        plugs.append(stats)

    lux_stats = db.metric_window_stats("lux", since, until)

    return {
        "from": since,
        "to": until,
        "duration_s": round(until - since),
        # The one field the dashboard leads on. "Nothing happened" must be as
        # clear as the alarming case, so this is an explicit boolean rather
        # than something the frontend infers from empty lists.
        "disturbed": bool(motion_events) or co2_rose or any(p["changed"] for p in plugs),
        "motion": {
            "events": len(motion_events),
            "samples": len(motion_ts),
            "times": [e["start"] for e in motion_events[:20]],
        },
        # A person sitting still defeats a PIR, which senses heat *movement*
        # across its field. They cannot defeat CO2. The two fail differently,
        # which is exactly why both are here. Omitted entirely when the window
        # holds no valid CO2 — a broken sensor must not read as "all clear".
        "co2": None if co2_stats["max"] is None else {
            "max": round(co2_stats["max"]),
            "start": round(co2_start) if co2_start is not None else None,
            "end": round(co2_end) if co2_end is not None else None,
            "delta": co2_delta,
            "rose_significantly": co2_rose,
        },
        "lux": {"max": lux_stats["max"]},
        "temp": db.metric_window_stats("temp", since, until),
        "hum": db.metric_window_stats("hum", since, until),
        "plugs": plugs,
    }


# ------------------------------------------------------------ morning summary

def _night_awakenings(since: float, until: float) -> list[dict]:
    """Motion during the Sleeping window, clustered into times you got up."""
    stamps = [e["ts"] for e in db.motion_events(since, limit=2000, until=until)]
    return cluster_events(stamps, config.DISTURBANCE_COOLDOWN_S)


def _compute_sleep_summary(since: float, until: float) -> dict:
    """Overnight stats from the existing readings table, plus the planner's
    look at the day being woken into — computed once at the Sleeping -> Home
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
        # Times you got out of bed, not raw PIR rows. A detection holds for as
        # long as the sensor keeps seeing you, so a raw count mostly measures
        # how long you were up and is not comparable night to night; clustered,
        # "3" means three separate times. Getting up and coming back inside
        # DISTURBANCE_COOLDOWN_S is deliberately one awakening.
        #
        # NOT the same thing as the Health module's Awakenings sub-score, which
        # comes from Apple Health sleep stages — you can wake without getting
        # up, so this is a strict subset measured a different way. Kept
        # separate on purpose; conflating them would corrupt a scored input.
        "motion": {
            "count": len(_night_awakenings(since, until)),
            "samples": db.motion_count(since, until),
            "events": [e["start"] for e in _night_awakenings(since, until)[:20]],
        },
        # today's events + overdue/high-priority tasks — same summary, one
        # more section, so the morning card stays a single report
        "planner": planner.morning_snapshot(until),
        # last night's recovery + sleep scores (None before any health data,
        # and on summaries stored before the health module existed)
        "health": health.morning_snapshot(until),
    }
