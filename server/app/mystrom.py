"""myStrom WiFi Switch client — local REST API only, no cloud.

Endpoints used (myStrom Switch, local HTTP on port 80):
  GET /report          -> {"power": <watts>, "relay": <bool>, ...}
  GET /relay?state=0|1 -> set relay explicitly
  GET /toggle          -> flip relay, returns {"relay": <bool>}

With MOCK_HARDWARE=1, MockPlug simulates a plug (toggleable state, a
plausible wobbling power draw when on) so the Power tab works end to end
without hardware.
"""

import logging
import random
import threading

import requests

from . import config

log = logging.getLogger("mystrom")

TIMEOUT_S = 3


class PlugError(Exception):
    """Plug unreachable or returned garbage."""


class MyStromPlug:
    def __init__(self, ip: str):
        self.ip = ip

    def _get(self, path: str) -> dict:
        url = f"http://{self.ip}{path}"
        try:
            resp = requests.get(url, timeout=TIMEOUT_S)
            resp.raise_for_status()
            return resp.json() if resp.text.strip() else {}
        except requests.RequestException as exc:
            raise PlugError(f"myStrom plug at {self.ip} unreachable: {exc}") from exc
        except ValueError as exc:
            raise PlugError(f"myStrom plug at {self.ip} sent invalid JSON") from exc

    def report(self) -> dict:
        """-> {"relay_on": bool, "watts": float}"""
        data = self._get("/report")
        return {"relay_on": bool(data.get("relay")), "watts": float(data.get("power", 0.0))}

    def toggle(self) -> bool:
        """Flip the relay; returns the new state."""
        data = self._get("/toggle")
        return bool(data.get("relay"))

    def set_state(self, on: bool) -> None:
        self._get(f"/relay?state={1 if on else 0}")


class MockPlug:
    """Fake plug for MOCK_HARDWARE=1. Each instance gets its own base load
    so multiple plugs look distinct on the dashboard."""

    def __init__(self, ip: str):
        self.ip = ip
        self._on = random.random() < 0.7
        self._base_watts = random.uniform(15, 80)
        self._lock = threading.Lock()
        log.warning("MOCK_HARDWARE=1: using fake myStrom plug (ip %s ignored)", ip)

    def report(self) -> dict:
        with self._lock:
            watts = round(self._base_watts * random.uniform(0.92, 1.08), 1) if self._on else 0.0
            return {"relay_on": self._on, "watts": watts}

    def toggle(self) -> bool:
        with self._lock:
            self._on = not self._on
            return self._on

    def set_state(self, on: bool) -> None:
        with self._lock:
            self._on = on


def make_plug(ip: str):
    return MockPlug(ip) if config.MOCK_HARDWARE else MyStromPlug(ip)
