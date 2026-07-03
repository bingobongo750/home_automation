# Server — Flask backend

Reads the Arduino over USB serial, polls the myStrom plug over the LAN,
stores everything in SQLite, and serves the JSON API plus the dashboard.

## Run locally

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp ../.env.example ../.env   # then edit values (serial port, plug IP, DB path)
python run.py
```

Dashboard: http://localhost:8000 — API under http://localhost:8000/api/...

## No hardware connected yet?

Set `MOCK_HARDWARE=1` (in `.env` or the environment):

```bash
MOCK_HARDWARE=1 python run.py
```

This fakes all three lanes — plausible drifting sensor readings on the
firmware's 5s cadence, a simulated plug (toggleable, ~60W when on), and
simulated WLED zones (toggleable, brightness/color/effect held in memory) —
so the full dashboard is developable with zero hardware. No serial port,
plug IP, or zone IP needs to be valid in mock mode.

With real hardware, failures are loud but non-fatal: if the serial port can't
be opened (or the USB drops) the reader logs `SERIAL UNAVAILABLE`/`SERIAL
DROPPED` and retries every 5s; if the plug doesn't answer, the poller logs
`PLUG UNREACHABLE` and keeps trying; if a WLED zone doesn't answer, the
auto-lighting job logs `WLED ZONE UNREACHABLE` and keeps trying. Sensor
ingestion, plug polling, and the auto-lighting job run on independent
threads and never block each other.

## Layout

| File | Role |
|---|---|
| `run.py` | entry point |
| `app/config.py` | env vars + minimal `.env` loader |
| `app/db.py` | SQLite schema and all queries (readings, devices, power_readings) |
| `app/serial_reader.py` | serial thread: `KEY:VALUE` lines → DB; command sending |
| `app/mystrom.py` | myStrom local REST client (+ mock) |
| `app/poller.py` | plug polling thread → DB |
| `app/wled.py` | WLED zone local JSON API client (+ mock) |
| `app/lighting.py` | auto-lighting thread: lux → brightness for zones in `auto` mode |
| `app/api.py` | REST endpoints |

## API

| Endpoint | Description |
|---|---|
| `GET /api/sensors/latest` | most recent reading per metric |
| `GET /api/sensors/history?metric=temp&range=24h` | downsampled time series (`m`/`h`/`d` ranges) |
| `GET /api/sensors/stats?metric=temp` | 24h min/max/avg + 7d avg |
| `GET /api/sensors/profile?metric=temp` | "typical day": 7d avg per half-hour of day |
| `GET /api/motion/events?range=24h` | recent motion detections + count in range |
| `GET /api/devices` | device registry: plugs with last polled power, WLED zones with live state + mode |
| `GET /api/devices/:id` | device + last polled state/power (plug) or live state (WLED zone) |
| `POST /api/devices/:id/toggle` | flip a plug relay |
| `GET /api/devices/:id/power/history?range=24h` | plug power draw series |
| `GET /api/devices/:id/power/stats` | plug 24h/7d avg draw + est. 24h kWh |
| `POST /api/devices/:id/state` | set a WLED zone's on/brightness/color/effect |
| `POST /api/devices/:id/mode` | set a WLED zone's mode: `manual` or `auto` |
| `GET/PUT /api/settings/thresholds` | alert thresholds (persisted in the DB) |
| `POST /api/arduino/command` | send a raw protocol line to the Arduino |

Plugs are seeded from `MYSTROM_PLUG_IP` / `MYSTROM_PLUG2_IP` in `.env`; to add
a third, extend `PLUG_SEEDS` in `app/db.py` and add its IP to `.env`. WLED
zones work the same way via `WLED_CUPBOARD_IP` / `WLED_TABLE_IP` and
`WLED_SEEDS`. Auto-lighting behavior (poll interval, lux threshold, target
brightness) is tuned via `LIGHTING_POLL_INTERVAL` / `LIGHTING_LUX_THRESHOLD`
/ `LIGHTING_AUTO_BRIGHTNESS` in `.env`.

The DB path defaults to `./data/home.db` (gitignored); point `DB_PATH` at the
1TB SSD for production, e.g. `/Volumes/SSD/home_automation/sensors.db`.
