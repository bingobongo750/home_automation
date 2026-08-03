"""Shelly Multicolor Bulb E27 Gen3 client — local JSON-RPC only, no cloud.

Each ambient-lighting zone is a single self-contained WiFi RGBW bulb screwed
into an ordinary E27 fixture (the fixture only ever supplies mains power).
Bulbs are WiFi devices, the same lane as the myStrom plug — never routed
through the Arduino/serial protocol.

Endpoints used (Shelly Gen2+ JSON-RPC over HTTP, port 80):
  POST /rpc  {"id": 1, "method": "RGBCCT.GetStatus", "params": {"id": 0}}
             -> {"result": {"output": bool, "brightness": 1-100,
                            "rgb": [r, g, b], "ct": <kelvin>, "mode": "rgb"|"cct"}}
  POST /rpc  {"id": 1, "method": "RGBCCT.Set", "params": {"id": 0, ...}}
             -> merges the given params into the bulb's current state

This bulb exposes an `rgbcct:0` component, so the methods are `RGBCCT.*` —
NOT the `Light.*` methods used by simpler Shelly dimmers. Component id is
always 0 on a single-light bulb.

**Brightness scale.** The hub is 0-255 everywhere (LIGHTING_AUTO_BRIGHTNESS,
the dashboard slider, api.py validation, scene targets); Shelly is 1-100
percent. The conversion lives here and nowhere else — every other layer keeps
speaking 0-255.

If a device password is set in the Shelly web UI, RPC requires digest auth,
which this client does not implement — leave it unset on a Tailscale-only LAN.

With MOCK_HARDWARE=1, MockShellyBulb simulates a bulb (on/off, brightness,
color held in memory) so the Lighting section works end to end without
hardware.
"""

import logging
import threading

import requests

from . import config

log = logging.getLogger("shelly")

TIMEOUT_S = 3
DEFAULT_COLOR = (255, 176, 102)  # warm white, matches the dashboard's copper accent
COMPONENT_ID = 0                 # rgbcct:0 — single light per bulb


class BulbError(Exception):
    """Bulb unreachable or returned garbage."""


def _to_pct(brightness_255: int) -> int:
    """Hub 0-255 -> Shelly 1-100. Shelly rejects 0, so a hub brightness of 0
    floors to 1 %; "off" is expressed with the `on` field, never brightness."""
    return max(1, min(100, round(brightness_255 / 255 * 100)))


def _to_255(brightness_pct: float) -> int:
    """Shelly 1-100 -> hub 0-255."""
    return max(0, min(255, round(brightness_pct * 255 / 100)))


class ShellyBulb:
    def __init__(self, ip: str):
        self.ip = ip

    def _rpc(self, method: str, params: dict) -> dict:
        """One JSON-RPC call; returns the unwrapped `result` object."""
        url = f"http://{self.ip}/rpc"
        body = {"id": 1, "method": method, "params": params}
        try:
            resp = requests.post(url, json=body, timeout=TIMEOUT_S)
            resp.raise_for_status()
            data = resp.json() if resp.text.strip() else {}
        except requests.RequestException as exc:
            raise BulbError(f"Shelly bulb at {self.ip} unreachable: {exc}") from exc
        except ValueError as exc:
            raise BulbError(f"Shelly bulb at {self.ip} sent invalid JSON") from exc
        # A Gen2 device can answer 200 with a JSON-RPC error object instead of
        # a result — an unknown method or a bad parameter lands here, not in
        # raise_for_status, so it has to be checked explicitly.
        if "error" in data:
            raise BulbError(f"Shelly bulb at {self.ip} rejected {method}: {data['error']}")
        return data.get("result") or {}

    def state(self) -> dict:
        """-> {"on": bool, "brightness": 0-255, "color": [r, g, b]}"""
        result = self._rpc("RGBCCT.GetStatus", {"id": COMPONENT_ID})
        color = (result.get("rgb") or list(DEFAULT_COLOR))[:3]
        return {
            "on": bool(result.get("output", False)),
            "brightness": _to_255(result.get("brightness", 0)),
            "color": [int(c) for c in color],
        }

    def set_state(self, *, on: bool | None = None, brightness: int | None = None,
                  color: list | None = None) -> dict:
        """Push a partial update (only the given fields change); returns the
        bulb's resulting state so the caller doesn't need a second round trip."""
        params: dict = {"id": COMPONENT_ID}
        if on is not None:
            params["on"] = on
        if brightness is not None:
            params["brightness"] = _to_pct(brightness)
        if color is not None:
            params["rgb"] = [int(c) for c in color]
            # Without this the bulb stays in whatever mode it was in — a bulb
            # left in "cct" ignores rgb entirely and the color silently does
            # nothing.
            params["mode"] = "rgb"
        if len(params) > 1:  # "id" alone is a no-op call
            self._rpc("RGBCCT.Set", params)
        return self.state()


class MockShellyBulb:
    """Fake Shelly bulb for MOCK_HARDWARE=1. Holds hub-scale (0-255)
    brightness — the percent conversion is a real-transport concern."""

    def __init__(self, ip: str):
        self.ip = ip
        self._on = True
        self._brightness = 140
        self._color = list(DEFAULT_COLOR)
        self._lock = threading.Lock()
        log.warning("MOCK_HARDWARE=1: using fake Shelly bulb (ip %s ignored)", ip)

    def state(self) -> dict:
        with self._lock:
            return {"on": self._on, "brightness": self._brightness,
                    "color": list(self._color)}

    def set_state(self, *, on: bool | None = None, brightness: int | None = None,
                  color: list | None = None) -> dict:
        with self._lock:
            if on is not None:
                self._on = on
            if brightness is not None:
                self._brightness = brightness
            if color is not None:
                self._color = list(color)
            # inline, rather than calling self.state() — that also takes
            # self._lock, which is not reentrant
            return {"on": self._on, "brightness": self._brightness,
                    "color": list(self._color)}


def make_bulb(ip: str):
    return MockShellyBulb(ip) if config.MOCK_HARDWARE else ShellyBulb(ip)
