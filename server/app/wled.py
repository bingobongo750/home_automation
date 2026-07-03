"""WLED zone client — local JSON API only, no cloud.

Each lighting zone is a separate ESP32 running stock WLED firmware (open
source, https://kno.wled.ge). Zones are WiFi devices, the same lane as the
myStrom plug — never routed through the Arduino/serial protocol.

Endpoints used (WLED local HTTP JSON API, port 80):
  GET  /json/state  -> {"on": bool, "bri": 0-255,
                         "seg": [{"col": [[r,g,b], ...], "fx": <effect id>}]}
  POST /json/state  <- any subset of the above; WLED merges it into current state

With MOCK_HARDWARE=1, MockWledZone simulates a zone (on/off, brightness,
color, effect held in memory) so the Lighting section works end to end
without hardware.
"""

import logging
import threading

import requests

from . import config

log = logging.getLogger("wled")

TIMEOUT_S = 3
DEFAULT_COLOR = (255, 176, 102)  # warm white, matches the dashboard's copper accent


class WledError(Exception):
    """Zone unreachable or returned garbage."""


class WledZone:
    def __init__(self, ip: str):
        self.ip = ip

    def _get(self, path: str) -> dict:
        url = f"http://{self.ip}{path}"
        try:
            resp = requests.get(url, timeout=TIMEOUT_S)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise WledError(f"WLED zone at {self.ip} unreachable: {exc}") from exc
        except ValueError as exc:
            raise WledError(f"WLED zone at {self.ip} sent invalid JSON") from exc

    def _post(self, body: dict) -> None:
        url = f"http://{self.ip}/json/state"
        try:
            resp = requests.post(url, json=body, timeout=TIMEOUT_S)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise WledError(f"WLED zone at {self.ip} unreachable: {exc}") from exc

    def state(self) -> dict:
        """-> {"on": bool, "brightness": 0-255, "color": [r, g, b], "effect": int}"""
        data = self._get("/json/state")
        seg0 = (data.get("seg") or [{}])[0]
        color = (seg0.get("col") or [list(DEFAULT_COLOR)])[0][:3]
        return {
            "on": bool(data.get("on", False)),
            "brightness": int(data.get("bri", 0)),
            "color": list(color),
            "effect": int(seg0.get("fx", 0)),
        }

    def set_state(self, *, on: bool | None = None, brightness: int | None = None,
                  color: list | None = None, effect: int | None = None) -> dict:
        """Push a partial update (only the given fields change); returns the
        zone's resulting state so the caller doesn't need a second round trip."""
        body = {}
        if on is not None:
            body["on"] = on
        if brightness is not None:
            body["bri"] = brightness
        seg = {}
        if color is not None:
            seg["col"] = [list(color)]
        if effect is not None:
            seg["fx"] = effect
        if seg:
            body["seg"] = [seg]
        self._post(body)
        return self.state()


class MockWledZone:
    """Fake WLED zone for MOCK_HARDWARE=1."""

    def __init__(self, ip: str):
        self.ip = ip
        self._on = True
        self._brightness = 140
        self._color = list(DEFAULT_COLOR)
        self._effect = 0
        self._lock = threading.Lock()
        log.warning("MOCK_HARDWARE=1: using fake WLED zone (ip %s ignored)", ip)

    def state(self) -> dict:
        with self._lock:
            return {"on": self._on, "brightness": self._brightness,
                     "color": list(self._color), "effect": self._effect}

    def set_state(self, *, on: bool | None = None, brightness: int | None = None,
                  color: list | None = None, effect: int | None = None) -> dict:
        with self._lock:
            if on is not None:
                self._on = on
            if brightness is not None:
                self._brightness = brightness
            if color is not None:
                self._color = list(color)
            if effect is not None:
                self._effect = effect
            # inline, rather than calling self.state() — that also takes
            # self._lock, which is not reentrant
            return {"on": self._on, "brightness": self._brightness,
                    "color": list(self._color), "effect": self._effect}


def make_wled_zone(ip: str):
    return MockWledZone(ip) if config.MOCK_HARDWARE else WledZone(ip)
