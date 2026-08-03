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

# Colour-temperature limits of the Shelly Multicolor E27 Gen3's white channel.
# These are HARD limits, verified against the hardware: the bulb answers a `ct`
# outside them with JSON-RPC error -103 ("should be a number greater/equal than
# 2700 and less/equal than 6500") — it does NOT clamp. Anything sending `ct`
# must therefore clamp first or the call fails outright.
CT_MIN_K = 2700
CT_MAX_K = 6500
DEFAULT_CT_K = 2700

# Ambient light is CCT, not RGB. The bulb has a dedicated white channel plus
# separate R/G/B dies, and only one is lit at a time (`mode`). Synthesising
# warm white from the colour dies costs most of the output — measured on the
# real bulb at full brightness: cct 2700 K = 8.7 W, rgb (255,176,102) = 5.5 W,
# and the lumen gap is wider still because the RGB dies are much less
# efficacious per watt than a white phosphor. So the ambient/warmth control
# sends `ct` and the bulb runs its white channel; `rgb` is reserved for the
# dashboard's "Custom" colour picker, where being dimmer is an accepted cost.


def clamp_ct(kelvin: int) -> int:
    """Kelvin -> a value the bulb will actually accept (it errors, not clamps)."""
    return max(CT_MIN_K, min(CT_MAX_K, int(kelvin)))


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
        """-> {"on", "brightness" 0-255, "color" [r,g,b], "ct" K, "color_mode"}

        `color_mode` is the bulb's rgb/cct channel — deliberately NOT called
        "mode", which on a device row already means manual/auto lighting."""
        result = self._rpc("RGBCCT.GetStatus", {"id": COMPONENT_ID})
        color = (result.get("rgb") or list(DEFAULT_COLOR))[:3]
        return {
            "on": bool(result.get("output", False)),
            "brightness": _to_255(result.get("brightness", 0)),
            "color": [int(c) for c in color],
            "ct": int(result.get("ct") or DEFAULT_CT_K),
            "color_mode": result.get("mode") or "cct",
        }

    def set_state(self, *, on: bool | None = None, brightness: int | None = None,
                  color: list | None = None, ct: int | None = None) -> dict:
        """Push a partial update (only the given fields change); returns the
        bulb's resulting state so the caller doesn't need a second round trip.

        `ct` selects the white channel (ambient), `color` the RGB dies
        (custom). They are mutually exclusive — the bulb lights one channel at
        a time, so sending both would make the winner depend on `mode`, which
        is exactly the silent-surprise this argues against."""
        if color is not None and ct is not None:
            raise ValueError("set_state takes color or ct, not both — the bulb "
                             "lights either its RGB dies or its white channel")
        params: dict = {"id": COMPONENT_ID}
        if on is not None:
            params["on"] = on
        if brightness is not None:
            params["brightness"] = _to_pct(brightness)
        # `mode` is sent explicitly in both branches. The bulb does infer mode
        # from which colour field arrives, but a bulb left in the other mode
        # ignores the field entirely and the change silently does nothing.
        if color is not None:
            params["rgb"] = [int(c) for c in color]
            params["mode"] = "rgb"
        elif ct is not None:
            params["ct"] = clamp_ct(ct)
            params["mode"] = "cct"
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
        self._ct = DEFAULT_CT_K
        self._color_mode = "cct"   # ambient is the mostly-used mode
        self._lock = threading.Lock()
        log.warning("MOCK_HARDWARE=1: using fake Shelly bulb (ip %s ignored)", ip)

    def _snapshot(self) -> dict:
        return {"on": self._on, "brightness": self._brightness,
                "color": list(self._color), "ct": self._ct,
                "color_mode": self._color_mode}

    def state(self) -> dict:
        with self._lock:
            return self._snapshot()

    def set_state(self, *, on: bool | None = None, brightness: int | None = None,
                  color: list | None = None, ct: int | None = None) -> dict:
        if color is not None and ct is not None:
            raise ValueError("set_state takes color or ct, not both — the bulb "
                             "lights either its RGB dies or its white channel")
        with self._lock:
            if on is not None:
                self._on = on
            if brightness is not None:
                self._brightness = brightness
            if color is not None:
                self._color = list(color)
                self._color_mode = "rgb"
            elif ct is not None:
                # mirrors the real bulb, which refuses out-of-range ct outright
                self._ct = clamp_ct(ct)
                self._color_mode = "cct"
            # inline, rather than calling self.state() — that also takes
            # self._lock, which is not reentrant
            return self._snapshot()


def make_bulb(ip: str):
    return MockShellyBulb(ip) if config.MOCK_HARDWARE else ShellyBulb(ip)
