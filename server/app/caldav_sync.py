"""Apple Calendar import over CalDAV — read-only, one way.

WHY READ-ONLY. Two-way sync sounds better and is much worse: deletions,
conflicts, ETags and recurring-event exceptions are where CalDAV
implementations go wrong, and this planner's recurrence model is deliberately
`none|daily|weekly` rather than RFC 5545 (see app/planner.py), so writing back
would be lossy by construction. Imported events are therefore mirrors — the
dashboard renders them distinctly and refuses to edit them, and a re-import
replaces them wholesale. Nothing here ever writes to iCloud.

WHY THE PROTOCOL IS HAND-ROLLED. The `caldav` library pulls in lxml, an HTTP/3
stack and 16 packages in total to issue what amounts to four XML requests. This
box is a 4GB Pi whose whole design premise is lightweight I/O, so the four
requests live here on top of `requests`, which is already a dependency. What is
NOT hand-rolled is iCalendar parsing and RRULE expansion — recurrence rules are
a deep spec and getting them subtly wrong is worse than not having them, so
`icalendar` and `recurring_ical_events` (both pure-Python) do that part.

THE FLATTENING. Apple's RRULEs are expanded into concrete occurrences across a
rolling window and stored as individual rows. That sidesteps representing rules
the planner cannot express: the rows are read-only, so "edit the series" never
applies to them.

CREDENTIALS. CALDAV_PASSWORD must be an Apple **app-specific password**, never
the account password — scoped, revocable on its own, and it does not carry 2FA.
It lives in .env on the Pi, which does not travel to git (MANUAL.md 8.2).
"""

import logging
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests

from . import config, db, planner

log = logging.getLogger("caldav")

TIMEOUT_S = 20
DAV_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}

_lock = threading.RLock()
_last_result: dict = {"ok": None, "at": None, "imported": 0, "error": None,
                      "calendars": []}


class CalDavError(Exception):
    """Discovery, auth or fetch failed."""


def configured() -> bool:
    return bool(config.CALDAV_USERNAME and config.CALDAV_PASSWORD)


# ------------------------------------------------------------------ protocol

def _request(method: str, url: str, body: str | None, depth: str = "0") -> ET.Element:
    headers = {"Depth": depth, "Content-Type": 'application/xml; charset="utf-8"'}
    try:
        resp = requests.request(
            method, url, data=body.encode("utf-8") if body else None,
            headers=headers, timeout=TIMEOUT_S,
            auth=(config.CALDAV_USERNAME, config.CALDAV_PASSWORD),
        )
    except requests.RequestException as exc:
        raise CalDavError(f"{method} {url} failed: {exc}") from exc
    if resp.status_code == 401:
        raise CalDavError(
            "401 from iCloud — check CALDAV_USERNAME and that CALDAV_PASSWORD is an "
            "app-specific password from appleid.apple.com, not the account password")
    if resp.status_code >= 400:
        raise CalDavError(f"{method} {url} returned HTTP {resp.status_code}")
    try:
        return ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise CalDavError(f"{method} {url} returned unparseable XML: {exc}") from exc


def _absolute(base: str, href: str) -> str:
    """iCloud answers with paths, and redirects principals onto a partition
    host (pNN-caldav.icloud.com) — so hrefs must be resolved against whichever
    URL actually answered, not against the configured root."""
    return href if href.startswith("http") else urljoin(base, href)


def _find_href(root: ET.Element, path: str) -> str | None:
    node = root.find(path, DAV_NS)
    return node.text.strip() if node is not None and node.text else None


def discover_calendars() -> list[dict]:
    """-> [{"name", "url"}] for every calendar collection in the account."""
    principal_body = (
        '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/>'
        "</d:prop></d:propfind>")
    root = _request("PROPFIND", config.CALDAV_URL, principal_body)
    principal = _find_href(root, ".//d:current-user-principal/d:href")
    if not principal:
        raise CalDavError("no current-user-principal in the server's response")
    principal_url = _absolute(config.CALDAV_URL, principal)

    home_body = (
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><c:calendar-home-set/></d:prop></d:propfind>")
    root = _request("PROPFIND", principal_url, home_body)
    home = _find_href(root, ".//c:calendar-home-set/d:href")
    if not home:
        raise CalDavError("no calendar-home-set in the server's response")
    home_url = _absolute(principal_url, home)

    list_body = (
        '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
        "<d:displayname/></d:prop></d:propfind>")
    root = _request("PROPFIND", home_url, list_body, depth="1")

    calendars = []
    for resp in root.findall(".//d:response", DAV_NS):
        if resp.find(".//d:resourcetype/c:calendar", DAV_NS) is None:
            continue    # not a calendar collection (the home itself, todos, etc.)
        href = _find_href(resp, "d:href")
        name = _find_href(resp, ".//d:displayname") or "(unnamed)"
        if href:
            calendars.append({"name": name, "url": _absolute(home_url, href)})
    return calendars


def _fetch_ics(calendar_url: str, start: datetime, end: datetime) -> list[str]:
    """Time-ranged calendar-query. Asking the server to filter beats pulling
    every event ever and discarding most of it — years of history over a home
    connection, on every sync."""
    fmt = "%Y%m%dT%H%M%SZ"
    body = (
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><c:calendar-data/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VEVENT">'
        f'<c:time-range start="{start.strftime(fmt)}" end="{end.strftime(fmt)}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>")
    root = _request("REPORT", calendar_url, body, depth="1")
    out = []
    for node in root.findall(".//c:calendar-data", DAV_NS):
        if node.text:
            out.append(node.text)
    return out


# ------------------------------------------------------------------ mapping

def _to_epoch(value) -> tuple[float, bool]:
    """-> (epoch seconds, is_date_only). A bare DATE means all-day."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.timestamp(), False
        return value.timestamp(), False
    # datetime.date (not datetime) -> all-day, local midnight
    return datetime(value.year, value.month, value.day).timestamp(), True


def occurrences_in_window(ics_blobs: list[str], start: datetime,
                          end: datetime) -> list[dict]:
    """Expand every VEVENT (including RRULEs) into concrete occurrences."""
    import icalendar
    import recurring_ical_events

    found: list[dict] = []
    for blob in ics_blobs:
        try:
            cal = icalendar.Calendar.from_ical(blob)
        except Exception:
            log.warning("Skipping an unparseable calendar entry")
            continue
        try:
            events = recurring_ical_events.of(cal).between(start, end)
        except Exception:
            log.warning("Skipping an entry whose recurrence could not be expanded")
            continue
        for ev in events:
            try:
                dtstart = ev.get("DTSTART").dt
            except Exception:
                continue
            start_ts, all_day = _to_epoch(dtstart)
            end_ts = None
            if ev.get("DTEND") is not None:
                end_ts, _ = _to_epoch(ev.get("DTEND").dt)
            title = str(ev.get("SUMMARY") or "(no title)")
            notes = str(ev.get("DESCRIPTION") or "") or None
            uid = str(ev.get("UID") or "")
            found.append({
                "title": title, "start_ts": start_ts, "end_ts": end_ts,
                "notes": notes, "all_day": all_day, "external_uid": uid,
            })
    found.sort(key=lambda e: e["start_ts"])
    return found


# ------------------------------------------------------------------- import

def _signature(rows: list[dict]) -> str:
    """Cheap content hash. A calendar that has not changed must not rewrite the
    table every 15 minutes — this box writes to an SD card, and needless churn
    is the one thing that actually kills them (MANUAL.md 7.1)."""
    import hashlib
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['external_uid']}|{r['start_ts']}|{r['end_ts']}|"
                 f"{r['title']}|{r['all_day']}".encode("utf-8"))
    return h.hexdigest()


def sync() -> dict:
    """Fetch, expand and mirror. Returns a result dict; never raises."""
    global _last_result
    if not configured():
        return {"ok": False, "error": "CalDAV is not configured", "imported": 0}

    now = time.time()
    start = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(
        days=config.CALDAV_WINDOW_PAST_DAYS)
    end = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(
        days=config.CALDAV_WINDOW_FUTURE_DAYS)

    with _lock:
        try:
            calendars = discover_calendars()
            wanted = [c.strip() for c in config.CALDAV_CALENDARS.split(",") if c.strip()]
            if wanted:
                calendars = [c for c in calendars if c["name"] in wanted]
                missing = set(wanted) - {c["name"] for c in calendars}
                if missing:
                    log.warning("CALDAV_CALENDARS names not found in the account: %s",
                                ", ".join(sorted(missing)))
            if not calendars:
                raise CalDavError("no calendars matched — check CALDAV_CALENDARS")

            blobs: list[str] = []
            for cal in calendars:
                blobs.extend(_fetch_ics(cal["url"], start, end))
            rows = occurrences_in_window(blobs, start, end)
        except CalDavError as exc:
            log.error("Calendar sync failed: %s", exc)
            _last_result = {"ok": False, "at": now, "imported": 0,
                            "error": str(exc), "calendars": []}
            return _last_result
        except Exception as exc:                       # never kill the thread
            log.exception("Calendar sync failed unexpectedly")
            _last_result = {"ok": False, "at": now, "imported": 0,
                            "error": repr(exc), "calendars": []}
            return _last_result

        signature = _signature(rows)
        previous = db.get_setting("caldav_signature")
        if previous == signature:
            log.info("Calendar unchanged (%d occurrences) — no rewrite", len(rows))
            _last_result = {"ok": True, "at": now, "imported": len(rows),
                            "error": None, "unchanged": True,
                            "calendars": [c["name"] for c in calendars]}
            return _last_result

        # Replace wholesale rather than diffing: it is the only thing that
        # handles upstream deletions and edited times correctly, and these rows
        # are mirrors so there is nothing local to preserve.
        with db.connect() as conn:
            conn.execute("DELETE FROM events WHERE source = 'caldav'")
            conn.executemany(
                """INSERT INTO events
                       (title, start_ts, end_ts, notes, recurrence, category,
                        all_day, external_uid, source, created_at)
                   VALUES (?, ?, ?, ?, 'none', NULL, ?, ?, 'caldav', ?)""",
                [(r["title"], r["start_ts"], r["end_ts"], r["notes"],
                  1 if r["all_day"] else 0, r["external_uid"], now) for r in rows],
            )
        db.set_setting("caldav_signature", signature)
        log.info("Calendar sync imported %d occurrences from %d calendar(s)",
                 len(rows), len(calendars))
        _last_result = {"ok": True, "at": now, "imported": len(rows),
                        "error": None, "unchanged": False,
                        "calendars": [c["name"] for c in calendars]}
        return _last_result


def status() -> dict:
    return {"configured": configured(), "interval_s": config.CALDAV_SYNC_INTERVAL,
            **_last_result}


def _loop() -> None:
    while True:
        try:
            sync()
        except Exception:
            log.exception("Calendar sync loop caught an error")
        time.sleep(config.CALDAV_SYNC_INTERVAL)


def start() -> threading.Thread | None:
    """Its own thread on its own interval — not the plug poller, not the serial
    reader. An unreachable iCloud must never stall either of those."""
    if not configured():
        log.info("CalDAV not configured (no CALDAV_USERNAME) — calendar import off")
        return None
    thread = threading.Thread(target=_loop, name="caldav-sync", daemon=True)
    thread.start()
    log.info("Calendar sync every %ss, window -%dd..+%dd",
             config.CALDAV_SYNC_INTERVAL, config.CALDAV_WINDOW_PAST_DAYS,
             config.CALDAV_WINDOW_FUTURE_DAYS)
    return thread
