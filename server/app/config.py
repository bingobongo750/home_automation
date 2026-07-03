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
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
MOCK_HARDWARE = os.environ.get("MOCK_HARDWARE", "0") == "1"
