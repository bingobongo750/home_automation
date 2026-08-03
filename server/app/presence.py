"""Phone-driven presence: automatic Away on departure, Home or Sleeping on
return. Built to docs/presence-design.md.

THE SPLIT: the phone reports presence, the host decides the scene. Two iPhone
Shortcuts POST "I left" / "I came back" and nothing else. Which scene that
becomes — and whether a summary is worth showing — is decided here, so the
phone holds no copy of the sleep schedule to drift out of sync with, and
changing the schedule never means editing a Shortcut. Same split as the serial
protocol, where the Arduino reports readings and the host decides what they
mean.

FOUR RULES, and each one exists because of a specific failure:

  1. Away is the strongest state. Nothing automatic overrides it — not the
     nightly bedtime timer (guarded in scenes._fire_bedtime), not a pending
     wake (cancelled by activating Away). Only an arrival, or the user picking
     a scene by hand.

  2. Departure is delayed, arrival is immediate. A geofence that bounces would
     otherwise strobe the room. Arrival gets no grace because the whole point
     is lights on when you walk in.

  3. Arrival only ever ends Away. If the house is in Home or Sleeping, arrival
     is a no-op — that is what stops a spurious geofence event from overriding
     a scene chosen deliberately.

  4. A departure grace timer is NOT restored across a restart. If the hub was
     down through the departure, re-arming a stale timer to darken the house
     minutes later is worse than doing nothing.

This module imports scenes, never the reverse.
"""

import logging
import threading
import time
from datetime import datetime

from . import config, db, scenes

log = logging.getLogger("presence")

_lock = threading.RLock()
_depart_timer: threading.Timer | None = None
_depart_generation = 0  # bumped on every arm/cancel; a stale timer sees a mismatch


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    if not isinstance(value, str) or not scenes.WAKE_TIME_RE.match(value):
        return None
    hh, mm = value.split(":")
    return int(hh), int(mm)


def scene_for_arrival(now: float | None = None) -> tuple[str, str | None]:
    """-> (scene_name, wake_time) for an arrival at `now`.

    Sleeping if the arrival lands inside the stored nightly window, else Home.
    Arriving into the window carries the schedule's own wake_time, exactly as
    the bedtime timer does, so the normal Sleeping->Home machinery still runs in
    the morning — getting home at 02:00 must not cost you the morning summary.
    """
    schedule = db.get_sleep_schedule()
    if not schedule.get("enabled"):
        return "Home", None

    start = _parse_hhmm(schedule.get("sleep_time", ""))
    end = _parse_hhmm(schedule.get("wake_time", ""))
    if start is None or end is None:
        log.error("Nightly schedule has a bad time (%r -> %r); treating arrival as Home",
                  schedule.get("sleep_time"), schedule.get("wake_time"))
        return "Home", None
    if start == end:
        return "Home", None  # zero-length window

    local = datetime.fromtimestamp(now if now is not None else time.time())
    minutes = local.hour * 60 + local.minute
    start_m = start[0] * 60 + start[1]
    end_m = end[0] * 60 + end[1]

    inside = (start_m <= minutes < end_m) if start_m < end_m else (
        minutes >= start_m or minutes < end_m)   # window wraps past midnight
    if inside:
        return "Sleeping", schedule["wake_time"]
    return "Home", None


def _cancel_depart_locked() -> None:
    global _depart_timer, _depart_generation
    _depart_generation += 1
    if _depart_timer is not None:
        _depart_timer.cancel()
        _depart_timer = None


def _fire_depart(generation: int) -> None:
    """Grace period elapsed with no arrival — the departure is real."""
    with _lock:
        if generation != _depart_generation:
            log.info("Stale departure timer ignored (arrival cancelled it)")
            return
        global _depart_timer
        _depart_timer = None
        now = time.time()
        active = db.get_active_scene()
        if active and active["name"] == "Away":
            log.info("Departure confirmed but the house is already Away")
        else:
            log.info("Departure confirmed after %ss — activating Away",
                     config.PRESENCE_DEPART_GRACE_S)
            try:
                scenes.activate("Away", source="presence: departed")
            except Exception:
                log.exception("Presence could not activate Away")
                return
        presence = db.get_presence()
        # Keep the original `since` if we were already away, so the summary
        # window still covers the whole absence.
        since = presence["since"] if presence["state"] == "away" else now
        db.set_presence("away", since, "departed", now)


def departed() -> dict:
    """Phone left the geofence. Confirmed after the grace period, not now."""
    global _depart_timer, _depart_generation
    now = time.time()
    with _lock:
        presence = db.get_presence()
        if presence["state"] == "away":
            log.info("Departure ignored — already away since %.0f", presence["since"] or 0)
            return {"presence": "away", "applied": False,
                    "reason": "already away"}
        if _depart_timer is not None:
            # Do NOT restart the countdown: repeated triggers while walking out
            # of range would push Away further and further out.
            return {"presence": "home", "applied": False,
                    "reason": "departure already pending"}

        _depart_generation += 1
        _depart_timer = threading.Timer(config.PRESENCE_DEPART_GRACE_S,
                                        _fire_depart, args=(_depart_generation,))
        _depart_timer.daemon = True
        _depart_timer.name = "presence-depart"
        _depart_timer.start()
        db.set_presence("home", presence["since"], "departed", now)

    log.info("Departure registered — Away in %ss unless you come back first",
             config.PRESENCE_DEPART_GRACE_S)
    return {"presence": "home", "applied": False,
            "reason": f"departure pending, Away in {config.PRESENCE_DEPART_GRACE_S}s",
            "pending_departure_at": now + config.PRESENCE_DEPART_GRACE_S}


def arrived() -> dict:
    """Phone came back. Applied immediately, but only ends Away."""
    now = time.time()
    with _lock:
        had_pending = _depart_timer is not None
        _cancel_depart_locked()
        presence = db.get_presence()
        active = db.get_active_scene()
        scene_now = active["name"] if active else "Home"

        db.set_presence("home", None, "arrived", now)

        if had_pending and scene_now != "Away":
            # The bounce case: nothing was applied, so there is nothing to
            # undo. Silent on purpose.
            log.info("Arrival cancelled a pending departure (geofence bounce)")
            return {"presence": "home", "scene": scene_now, "applied": False,
                    "reason": "cancelled a pending departure"}

        if scene_now != "Away":
            log.info("Arrival ignored — house is in %s, not Away", scene_now)
            return {"presence": "home", "scene": scene_now, "applied": False,
                    "reason": f"house is in {scene_now}, not Away"}

        target, wake_time = scene_for_arrival(now)

        # The away summary is NOT computed here. scenes.activate() owns it, so
        # it fires on every way out of Away — a phone arrival, or you tapping
        # Home on the dashboard because this Shortcut never made it. One owner,
        # one window definition, no chance of the two drifting apart.
        try:
            result = scenes.activate(target, wake_time, source="presence: arrived")
        except Exception:
            log.exception("Presence could not activate %s on arrival", target)
            return {"presence": "home", "scene": scene_now, "applied": False,
                    "reason": "scene activation failed"}

    log.info("Arrival — %s activated%s", target,
             f", wake to Home at {wake_time}" if wake_time else "")
    return {"presence": "home", "scene": target, "applied": True,
            "reason": ("arrived inside the nightly sleep window"
                       if target == "Sleeping" else "arrived outside the sleep window"),
            "summary_generated": bool(result and result.get("away_summary_generated"))}


def state() -> dict:
    with _lock:
        presence = db.get_presence()
        pending = _depart_timer is not None
    return {**presence, "departure_pending": pending}
