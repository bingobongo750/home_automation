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
MYSTROM_PLUG_IP = os.environ.get("MYSTROM_PLUG_IP", "192.168.0.51")
MYSTROM_PLUG2_IP = os.environ.get("MYSTROM_PLUG2_IP", "192.168.0.52")
MYSTROM_POLL_INTERVAL = float(os.environ.get("MYSTROM_POLL_INTERVAL", "10"))

# Shelly bulb zones (ambient lighting) — WiFi devices, same lane as the
# myStrom plugs above. Placeholder IPs until each bulb is physically set up.
SHELLY_CUPBOARD_IP = os.environ.get("SHELLY_CUPBOARD_IP", "192.168.0.61")
SHELLY_ROOM_LED_IP = os.environ.get("SHELLY_ROOM_LED_IP", "192.168.0.62")

# Auto-lighting job: how often it re-checks lux and pushes brightness, the
# lux level below which a zone in 'auto' mode is considered "dark", and the
# brightness (0-255) it sets when dark (0 = off when it's bright enough).
LIGHTING_POLL_INTERVAL = float(os.environ.get("LIGHTING_POLL_INTERVAL", "30"))
LIGHTING_LUX_THRESHOLD = float(os.environ.get("LIGHTING_LUX_THRESHOLD", "50"))
LIGHTING_AUTO_BRIGHTNESS = int(os.environ.get("LIGHTING_AUTO_BRIGHTNESS", "180"))

# Presence (see docs/presence-design.md). A phone posts departure/arrival; the
# host decides the scene.
#   DEPART_GRACE  a departure is confirmed only after this long, so a bouncing
#                 geofence cannot strobe the room. Arrival has no grace — the
#                 point is lights on when you walk in.
#   ARRIVAL_TRIM  the away summary window ends this long BEFORE the detected
#                 arrival. You reach the door before the hub knows, so the last
#                 stretch is you: PIR, CO2, lux and plug draw all read as
#                 "disturbances" otherwise, and every homecoming looks like a
#                 break-in. Erring long costs a couple of minutes of an absence
#                 that was already hours.
#   SUMMARY_MIN   shorter absences produce no summary at all.
#   DISTURBANCE_COOLDOWN  repeated detections within this gap are ONE event. A
#                 PIR emits a sample per cycle while it sees movement, so
#                 counting raw rows reports "47 disturbances" for one person
#                 crossing the room.
PRESENCE_DEPART_GRACE_S = int(os.environ.get("PRESENCE_DEPART_GRACE_S", "120"))
PRESENCE_ARRIVAL_TRIM_S = int(os.environ.get("PRESENCE_ARRIVAL_TRIM_S", "120"))
PRESENCE_SUMMARY_MIN_S = int(os.environ.get("PRESENCE_SUMMARY_MIN_S", "600"))
DISTURBANCE_COOLDOWN_S = int(os.environ.get("DISTURBANCE_COOLDOWN_S", "300"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
MOCK_HARDWARE = os.environ.get("MOCK_HARDWARE", "0") == "1"

# Health module (sleep/recovery, app/health.py). Rolling-baseline windows and
# the recovery-score weights/penalties — all env-tunable so scores can be
# recomputed from stored raw data without code changes (see health-build-plan).
HEALTH_BASELINE_LONG_DAYS = int(os.environ.get("HEALTH_BASELINE_LONG_DAYS", "60"))
HEALTH_BASELINE_SHORT_DAYS = int(os.environ.get("HEALTH_BASELINE_SHORT_DAYS", "7"))
HEALTH_BASELINE_WARMUP_NIGHTS = int(os.environ.get("HEALTH_BASELINE_WARMUP_NIGHTS", "14"))
# Recovery-score weights (HRV dominant, RHR secondary, respiratory minor).
HEALTH_W_HRV = float(os.environ.get("HEALTH_W_HRV", "0.60"))
HEALTH_W_RHR = float(os.environ.get("HEALTH_W_RHR", "0.25"))
HEALTH_W_RR = float(os.environ.get("HEALTH_W_RR", "0.15"))
# Flag penalties: "something is off" states subtracted after the linear model.
HEALTH_TEMP_DEV_C = float(os.environ.get("HEALTH_TEMP_DEV_C", "0.5"))    # |wrist temp - baseline|
HEALTH_SPO2_DIP_PCT = float(os.environ.get("HEALTH_SPO2_DIP_PCT", "93")) # SpO2 below this = dip
HEALTH_RR_SPIKE_BR = float(os.environ.get("HEALTH_RR_SPIKE_BR", "1.0"))  # breaths/min over baseline
HEALTH_PENALTY_TEMP = float(os.environ.get("HEALTH_PENALTY_TEMP", "10"))
HEALTH_PENALTY_SPO2 = float(os.environ.get("HEALTH_PENALTY_SPO2", "8"))
HEALTH_PENALTY_RR = float(os.environ.get("HEALTH_PENALTY_RR", "6"))

# Sleep score (pass 5). Personal sleep "need" (minutes) — the default 8h is
# user-overridable via PUT /api/health/settings. Sub-score weights sum to 100;
# the rest are curve breakpoints. Everything env-tunable so a weight change is
# just a recompute over stored raw.
HEALTH_SLEEP_NEED_MIN = float(os.environ.get("HEALTH_SLEEP_NEED_MIN", "480"))
HEALTH_SW_DURATION = float(os.environ.get("HEALTH_SW_DURATION", "35"))
HEALTH_SW_WASO = float(os.environ.get("HEALTH_SW_WASO", "20"))
HEALTH_SW_CONSISTENCY = float(os.environ.get("HEALTH_SW_CONSISTENCY", "17"))
HEALTH_SW_REM = float(os.environ.get("HEALTH_SW_REM", "12"))
HEALTH_SW_AWAKENINGS = float(os.environ.get("HEALTH_SW_AWAKENINGS", "8"))
HEALTH_SW_DEEP = float(os.environ.get("HEALTH_SW_DEEP", "8"))
HEALTH_REM_TYPICAL = float(os.environ.get("HEALTH_REM_TYPICAL", "0.22"))  # fallback before a baseline
HEALTH_DEEP_TYPICAL = float(os.environ.get("HEALTH_DEEP_TYPICAL", "0.15"))
HEALTH_WASO_GOOD_MIN = float(os.environ.get("HEALTH_WASO_GOOD_MIN", "20"))  # full marks at/below
HEALTH_WASO_BAD_MIN = float(os.environ.get("HEALTH_WASO_BAD_MIN", "90"))    # zero at/above
HEALTH_AWK_GOOD = float(os.environ.get("HEALTH_AWK_GOOD", "1"))
HEALTH_AWK_BAD = float(os.environ.get("HEALTH_AWK_BAD", "8"))
HEALTH_DUR_SHORT_PENALTY_PER_H = float(os.environ.get("HEALTH_DUR_SHORT_PENALTY_PER_H", "35"))  # duration pts lost per hour under need
HEALTH_OVERSLEEP_TOL_MIN = float(os.environ.get("HEALTH_OVERSLEEP_TOL_MIN", "60"))
HEALTH_OVERSLEEP_ZERO_MIN = float(os.environ.get("HEALTH_OVERSLEEP_ZERO_MIN", "240"))
HEALTH_CONS_SD_BAD_MIN = float(os.environ.get("HEALTH_CONS_SD_BAD_MIN", "120"))  # timing SD -> 0

# Deep-dive sleep metrics (pass 6).
HEALTH_SLEEP_DEBT_DAYS = int(os.environ.get("HEALTH_SLEEP_DEBT_DAYS", "14"))
HEALTH_SLEEP_SURPLUS_DISCOUNT = float(os.environ.get("HEALTH_SLEEP_SURPLUS_DISCOUNT", "0.5"))
HEALTH_SLEEP_PAYBACK_ALPHA = float(os.environ.get("HEALTH_SLEEP_PAYBACK_ALPHA", "0.5"))
HEALTH_SLEEP_PAYBACK_CAP_MIN = float(os.environ.get("HEALTH_SLEEP_PAYBACK_CAP_MIN", "90"))
HEALTH_SRI_WINDOW_DAYS = int(os.environ.get("HEALTH_SRI_WINDOW_DAYS", "7"))
HEALTH_SRI_EPOCH_SEC = int(os.environ.get("HEALTH_SRI_EPOCH_SEC", "300"))
