# CLAUDE.md — Smart Home Hub

This file gives Claude Code persistent context for this repository. Read it before making
architectural decisions. It reflects real hardware/software choices already made — don't
relitigate them without a clear reason.

## Project summary

A DIY smart home hub. An old MacBook (8GB RAM) runs 24/7 as the central server, sensor
database, and web dashboard. An Arduino Uno handles all wired, time-sensitive I/O over a
single USB serial connection. WiFi devices (currently one myStrom smart plug) are controlled
directly by the host over the network — no cloud, no third-party hub, no Home Assistant.

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
   Currently: one **myStrom WiFi Switch** (Swiss Type J plug, local REST API, power
   monitoring 2–3680W). No cloud account or app is required for runtime control, only
   initial WiFi provisioning. Treat this integration as the first of potentially several
   WiFi devices — design the backend's device abstraction so a second WiFi plug or a
   WLED-based LED controller can be added without rewriting the plug logic.

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
- WS2812B/NeoPixel strip (CO2 traffic-light indicator, motion accent lighting, sunrise alarm)
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

## API shape (backend, minimum viable)

Design REST (or simple JSON) endpoints along these lines — adjust naming as needed, but
keep the shape:

- `GET /api/sensors/latest` — most recent reading per sensor
- `GET /api/sensors/history?metric=temp&range=24h` — time series for charts
- `GET /api/devices` — list known devices (currently: the myStrom plug)
- `GET /api/devices/:id` — device state + latest power draw
- `POST /api/devices/:id/toggle` — turn a WiFi plug on/off
- `GET /api/devices/:id/power/history` — power draw over time

## Frontend

- Dark theme, sleek, intentional — not a default Bootstrap/Tailwind look. Consult the
  `frontend-design` skill before writing UI code for aesthetic direction and design
  tokens.
- Tabbed layout, one tab per functional area, not one long scrolling dashboard:
  - **Room Conditions** — temp, humidity, light, CO2 (live values + short history charts)
  - **Power** — plug on/off control, current draw, power history/graph
  - **Motion / Presence** — PIR status, recent activity log
  - Reserve tab structure for later: **Lighting** (once WS2812B control ships)
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
