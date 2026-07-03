"""Configuration from environment variables, with a minimal .env loader.

A tiny stdlib loader is used instead of python-dotenv to keep the host
footprint at exactly three dependencies (flask, pyserial, requests).
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Load KEY=VALUE lines into os.environ (existing env vars win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


load_dotenv()

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/tty.usbmodem14101")
SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "115200"))
DB_PATH = os.environ.get("DB_PATH", str(REPO_ROOT / "data" / "home.db"))
MYSTROM_PLUG_IP = os.environ.get("MYSTROM_PLUG_IP", "192.168.1.50")
MYSTROM_PLUG2_IP = os.environ.get("MYSTROM_PLUG2_IP", "192.168.1.51")
MYSTROM_POLL_INTERVAL = float(os.environ.get("MYSTROM_POLL_INTERVAL", "10"))

# WLED zones (ambient lighting) — WiFi devices, same lane as the myStrom
# plugs above. Placeholder IPs until each zone's ESP32 is physically set up.
WLED_CUPBOARD_IP = os.environ.get("WLED_CUPBOARD_IP", "192.168.1.60")
WLED_TABLE_IP = os.environ.get("WLED_TABLE_IP", "192.168.1.61")

# Auto-lighting job: how often it re-checks lux and pushes brightness, the
# lux level below which a zone in 'auto' mode is considered "dark", and the
# brightness (0-255) it sets when dark (0 = off when it's bright enough).
LIGHTING_POLL_INTERVAL = float(os.environ.get("LIGHTING_POLL_INTERVAL", "30"))
LIGHTING_LUX_THRESHOLD = float(os.environ.get("LIGHTING_LUX_THRESHOLD", "50"))
LIGHTING_AUTO_BRIGHTNESS = int(os.environ.get("LIGHTING_AUTO_BRIGHTNESS", "180"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
MOCK_HARDWARE = os.environ.get("MOCK_HARDWARE", "0") == "1"
