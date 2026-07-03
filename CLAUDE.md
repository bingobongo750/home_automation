# CLAUDE.md — Smart Home Hub

This file gives Claude Code persistent context for this repository. Read it before making
architectural decisions. It reflects real hardware/software choices already made — don't
relitigate them without a clear reason.

## Project summary

A DIY smart home hub. An old MacBook (8GB RAM) runs 24/7 as the central server, sensor
database, and web dashboard. An Arduino Uno handles all wired, time-sensitive I/O over a
single USB serial connection. WiFi devices — currently two myStrom smart plugs and two
WLED ambient-lighting zones — are controlled directly by the host over the network — no
cloud, no third-party hub, no Home Assistant.

**Hard constraint:** the host is an 8GB RAM MacBook. Never introduce video transcoding,
AI/camera vision, Docker-heavy stacks, or anything with a large idle memory footprint.
Everything on the host should be lightweight I/O: read serial, write SQLite, serve HTTP,
call REST APIs.

## Two device lanes — do not blur these

1. **WIRED lane** (Arduino Uno ↔ Mac via USB serial)
   All sensors and any relay/MOSFET/LED-strip actuators wire into the Arduino via
   breadboard — never directly into the Mac. Time-sensitive or per-pixel timing logic
   (e.g. NeoPixel animation) lives entirely on the Arduino. The host only ever sends
   short, high-level text commands over serial and reads short structured lines back.
   Never send per-pixel or per-frame data over serial.

2. **WIRELESS lane** (Mac ↔ WiFi devices directly, bypassing the Arduino entirely)
   The host's server calls each WiFi device's local REST API directly over the LAN.
   Currently two device types:
   - Two **myStrom WiFi Switch** plugs (Swiss Type J, local REST API, power monitoring
     2–3680W) — "Plug 1" (Living Room) is physically installed, "Plug 2" is seeded in
     the `devices` table as a placeholder IP until it's physically installed.
   - Two **WLED ambient-lighting zones** ("Cupboard", "Table") — each a separate ESP32
     running stock WLED firmware (local JSON API, no cloud), driving an addressable LED
     strip near that zone. Both are seeded as placeholder IPs; no ESP32 is flashed yet.
   No cloud account or app is required for runtime control, only initial WiFi
   provisioning. Devices are modeled generically (see below) so further WiFi plugs or
   lighting zones can register without rewriting existing device logic. **Do not confuse
   this with the wired NeoPixel strip in the hardware inventory below** — that would be
   a strip wired directly into the Arduino, driven over serial; WLED zones are wireless
   ESP32 nodes and never touch the Arduino or the serial protocol.

## Hardware inventory (wired / Arduino side)

I2C bus, shared on pins A4/A5, no address conflicts:

| Sensor | Purpose | Interface | Address |
|---|---|---|---|
| BME280 | Temperature + humidity | I2C | 0x76 or 0x77 |
| BH1750 | Ambient light level | I2C | 0x23 |
| SCD30 / SCD40 | CO2 | I2C | 0x61 |
| HC-SR501 (PIR) | Motion | Digital pin | — |

Use Adafruit-style breakout boards (onboard 3.3V regulation / level shifting) — never bare
sensor chips — since the Uno's logic is 5V.

Planned/future wired additions (design for extensibility, don't build yet):
- WS2812B/NeoPixel strip wired directly to the Arduino (CO2 traffic-light indicator,
  motion accent lighting, sunrise alarm) via `MODE:`/`COLOR:` serial commands — distinct
  from the WLED ambient-lighting zones, which are wireless ESP32 nodes on the WIRELESS
  lane above, not this strip
- Opto-isolated relay module for mains ON/OFF switching
- Logic-level MOSFET (e.g. IRLZ44N) for low-voltage LED dimming
- OLED/e-ink display, RFID reader, water leak sensor, soil moisture sensor

## Serial protocol (host ⇄ Arduino)

Line-based, human-readable, terminated with `\n`. Parse with `Serial.readStringUntil('\n')`
on the Arduino side. Keep this protocol stable — both firmware and host code depend on it.

**Host → Arduino (commands):**
```
RELAY1:ON
RELAY1:OFF
DIM1:180                # PWM value 0-255
MODE:aqi                # NeoPixel mode select
COLOR:255,0,0           # NeoPixel direct color set
```

**Arduino → Host (telemetry, sent periodically, one reading per line):**
```
TEMP:21.4
HUM:47.2
LUX:312
CO2:612
MOTION:1
```

Prefer simple `KEY:VALUE` framing over JSON on the wire — the Uno has very little RAM and
JSON parsing libraries add overhead. The host is responsible for structuring/labeling data
before it hits the database or API layer.

## WiFi plug integration (myStrom)

- Local REST API only — no myStrom cloud calls at runtime.
- Host needs: device local IP (static DHCP reservation recommended), and knowledge of the
  myStrom local HTTP endpoints for state, toggle, and power-consumption reporting.
- Design the backend so plug polling (state + power draw) runs on its own lightweight
  interval, independent of the Arduino serial read loop — these are two unrelated event
  sources and should not block each other.
- Model devices generically in the backend (e.g. a `devices` table/interface with type
  `wifi_plug`, `ip`, `name`, `room`) so additional WiFi devices can register without new
  one-off code paths.
- Physical setup (provisioning the plug onto WiFi, assigning a static IP) happens later
  and is out of scope for the software work — but the backend, database schema, and API
  should already assume at least one WiFi plug exists and expose endpoints for it (state,
  toggle, power history) from day one.

## WLED lighting integration

- Each ambient-lighting zone is a separate ESP32 running stock **WLED** firmware
  (open source, local JSON API at `/json/state`, no cloud) — same device abstraction as
  the myStrom plug, `type = 'wled_zone'`, with an added `mode` column: `manual` or `auto`.
- `app/wled.py` is the client (get state; push a partial on/brightness/color/effect
  update) — mirrors `app/mystrom.py`'s shape, including a mock for `MOCK_HARDWARE=1`.
- `app/lighting.py` is a **separate** background thread (not the plug poller, not the
  serial reader) that, on its own interval (`LIGHTING_POLL_INTERVAL`), pushes a
  brightness update to every zone currently in `auto` mode based on the latest BH1750
  `lux` reading already in the DB: below `LIGHTING_LUX_THRESHOLD` it turns the zone on to
  `LIGHTING_AUTO_BRIGHTNESS`; at/above it, it turns the zone off. All three are env vars,
  not hardcoded. Zones in `manual` mode are left alone — the dashboard drives those
  directly via `POST /api/devices/:id/state`.
- Physical setup (flashing WLED, wiring the strip, static IP) happens later — the
  backend, database schema, and API already assume both zones exist from day one, same
  as the myStrom plug.

## Host stack

- Server: Python/Flask (lightweight, easy serial + REST integration, low idle footprint
  on 8GB RAM — preferred over Node/Express for this project unless you have a strong
  reason to deviate).
- Serial: `pyserial` for Arduino communication over USB.
- Database: SQLite for sensor time-series data and device state history.
- Remote access: Tailscale (assume it's available; don't build custom auth/tunneling).
- Storage: sensor DB, backups, and file sharing live on an already-mounted 1TB USB-C SSD
  — don't assume the DB has to live on the internal drive.
- No MQTT broker yet — single Arduino node for now. Keep the device/data layer decoupled
  enough that adding Mosquitto later for multi-room nodes doesn't require a rewrite.
- `MOCK_HARDWARE=1` env var (see `.env.example`) swaps the serial reader, the myStrom
  client, and the WLED client for fake data generators (plausible sinusoidal sensor
  drift, wobbling plug wattage, in-memory zone state) so the dashboard is developable
  end-to-end without any hardware attached. Keep this mode working when touching
  `serial_reader.py`, `mystrom.py`, or `wled.py`.

## API shape (backend)

Implemented in `server/app/api.py` — keep this list in sync when endpoints change:

- `GET /api/sensors/latest` — most recent reading per sensor
- `GET /api/sensors/history?metric=temp&range=24h` — time series for charts (range: `30m`/`24h`/`7d` style)
- `GET /api/sensors/stats?metric=temp` — 24h min/max/avg + 7d avg, for a widget's expanded view
- `GET /api/sensors/profile?metric=temp&bucket=30` — "typical day" curve: 7-day average per time-of-day bucket
- `GET /api/motion/events?range=24h` — recent motion detections + count, for the activity log
- `GET /api/devices` — list known devices (two myStrom plugs, two WLED zones); plug rows carry `power` (last polled sample), WLED rows carry `mode` and `light` (live state)
- `GET /api/devices/:id` — device row with the same per-type fields as above
- `POST /api/devices/:id/toggle` — turn a WiFi plug on/off
- `GET /api/devices/:id/power/stats` — 24h/7d average draw + estimated 24h kWh
- `GET /api/devices/:id/power/history` — power draw over time
- `POST /api/devices/:id/state` — set a WLED zone's on/brightness/color/effect (any subset)
- `POST /api/devices/:id/mode` — set a WLED zone's mode: `manual` or `auto`
- `GET/PUT /api/settings/thresholds` — alert thresholds (min/max per metric + plug power draw); a reading outside its band flags that widget on the dashboard
- `POST /api/arduino/command` — send a raw `KEY:VALUE` protocol line to the Arduino (exists for any future wired actuator and manual testing; WLED zones do not use this)

## Frontend

- Dark theme: a PCB soldermask/silkscreen look (deep green-black surfaces, copper accent,
  mono silkscreen labels, a live "RX" serial ticker replaying raw `KEY:VALUE` lines) — this
  visual language is approved, keep it when touching the dashboard. Consult the
  `frontend-design` skill for direction on any new surface.
- **Single-page layout, not tabs** — one scrolling page of widgets grouped into `.zone`
  sections: **Room conditions** (temp/humidity/light/CO2), **Power** (one plug-pair per
  WiFi plug: switch + power widget), **Lighting** (one card per WLED zone), **Motion**
  (PIR status + activity log). Any further device type should follow the same pattern:
  another `.zone` of widgets/cards on this same page, never a new tab.
- Each Lighting card's top-right switch is always the zone's physical on/off, and it
  stays clickable in both modes — a user can switch a zone off even while it's in
  `auto` (the lighting job may reassert brightness/on on its next tick, but a manual
  on/off click is never blocked by mode). Mode (`manual`/`auto`) is one of the control
  rows alongside brightness/color/effect. In `auto` mode only the brightness control
  goes read-only (the lighting job drives it from lux) but keeps displaying the live
  value every poll rather than freezing or disappearing; color/effect stay editable in
  either mode since the auto job never
  touches them.
- Each widget expands on click into an **overlay dialog** (detail view: range-scoped chart,
  min/max/avg stats, "typical now" 7d-avg-by-time-of-day) rather than expanding in place —
  in-place expansion was rejected because it reflowed the grid out from under the cursor.
  Never make widget interaction shift the board layout.
- An alert-thresholds settings dialog (gear icon) lets the min/max band per metric (and
  plug power draw) be edited; a reading outside its band flags that widget on the board.
- Keep it a single lightweight web app served by the host — no separate build
  infrastructure beyond what's needed for a small SPA or server-rendered pages.
- Live-ish updates (short polling interval or simple WebSocket) rather than manual
  refresh, but nothing resource-heavy — this is still constrained by the 8GB host.

## Conventions

- Keep firmware (`/firmware`) and host (`/server`, `/dashboard` or similar) cleanly
  separated in the repo.
- Comment the serial protocol wherever it's touched on either side — it's the seam most
  likely to drift out of sync between firmware and host if changed carelessly.
- Favor explicit, readable code over cleverness — this runs unattended 24/7; failures
  should be loud in logs, not silent.
- Don't add cloud dependencies, telemetry, or external services beyond what's already
  decided here (Tailscale, myStrom local API) without flagging it first.
