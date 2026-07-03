# API notes

Base URL: `http://<host>:8000/api`. All responses are JSON. Timestamps are
unix epoch seconds (UTC); the dashboard formats them client-side.

## Sensors

- `GET /sensors/latest` → `{"temp": {"value": 21.4, "ts": ...}, "hum": ..., ...}`
  — most recent reading per metric (`temp`, `hum`, `lux`, `co2`, `motion`).
- `GET /sensors/history?metric=temp&range=24h` →
  `{"metric": "temp", "points": [{"ts": ..., "value": ...}, ...]}`
  — `range` accepts `<n>m|h|d` (e.g. `30m`, `3h`, `7d`), default `24h`.
  Series are downsampled server-side to ≤ ~300 points by time-bucket
  averaging (bucket **max** for `motion`, so events survive averaging).
- `GET /sensors/profile?metric=temp` →
  `{"metric": ..., "days": 7, "points": [{"tod": ..., "value": ...}, ...]}`
  — the "typical day" curve: 7-day average per half-hour bucket of the day;
  `tod` is seconds since local midnight at the bucket center. The dashboard
  overlays this on the 24h chart and derives the "Typical now" stat from it.
- `GET /sensors/stats?metric=temp` →
  `{"min_24h", "max_24h", "avg_24h", "avg_7d"}` — summary stats for the
  dashboard's expanded widget view. `null` fields mean no data in that window.
- `GET /motion/events?range=24h` → `{"events": [{"ts": ...}, ...], "count": n}`
  newest first (max 50), for the activity log; `count` covers the whole range.

## Devices

Two device types exist so far, both WiFi (never routed through the
Arduino/serial lane): `wifi_plug` (myStrom) and `wled_zone` (ambient
lighting, stock WLED firmware on an ESP32 — see "Lighting" below).

- `GET /devices` → array of `{id, name, type, ip, room, ...}`. `wifi_plug`
  rows include `"power"` (last polled `{ts, watts, relay_on}`, or `null`).
  `wled_zone` rows include `"mode"` (`"manual"|"auto"`) and `"light"` (live
  `{on, brightness, color, effect}`, or `null` if the zone is unreachable).
  One call refreshes every plug widget and lighting card.
- `GET /devices/:id` → same per-type shape as one row above.
- `POST /devices/:id/toggle` → *wifi_plug only.* `{"relay_on": true|false}`
  (new state). `502` with `{"error": ...}` if the plug is unreachable.
- `GET /devices/:id/power/history?range=24h` → *wifi_plug only.*
  `{"device_id": ..., "points": [{"ts", "watts"}, ...]}` — same range/
  downsampling rules as sensor history.
- `GET /devices/:id/power/stats` → *wifi_plug only.*
  `{"avg_24h_w", "kwh_24h", "avg_7d_w"}` — `kwh_24h` is average draw
  integrated over the hours actually covered by samples, so it's an
  estimate (≈) rather than metered energy.

## Lighting (WLED zones)

- `POST /devices/:id/state` → *wled_zone only.* Body: any subset of
  `{"on": bool, "brightness": 0-255, "color": [r, g, b], "effect": <int>}`.
  Pushes a partial update to the zone and returns its resulting
  `{on, brightness, color, effect}`. `400` on out-of-range values, `502` if
  the zone is unreachable. Only meaningful in `manual` mode — in `auto`
  mode the lighting job (see below) overwrites `on`/`brightness` on its
  next tick.
- `POST /devices/:id/mode` → *wled_zone only.* Body: `{"mode": "manual"|"auto"}`.
  In `auto` mode, a background job (independent of sensor ingestion and
  plug polling — see `app/lighting.py`) reads the latest BH1750 `lux`
  reading every `LIGHTING_POLL_INTERVAL` seconds and pushes brightness to
  the zone: below `LIGHTING_LUX_THRESHOLD` it turns on to
  `LIGHTING_AUTO_BRIGHTNESS`; at/above it, it turns the zone off. All three
  are env vars (`.env`), not hardcoded.

## Settings

- `GET /settings/thresholds` → alert thresholds per key
  (`temp`, `hum`, `lux`, `co2`, `power`), each `{"min": ..., "max": ...}`
  with `null` meaning that bound is disabled. Defaults: temp 17–26 °C,
  hum 30–60 %RH, CO₂ max 1000 ppm, plug power max 1800 W, lux off.
- `PUT /settings/thresholds` → replace them (same shape as the GET; the
  dashboard's gear dialog uses this). Rejects `min >= max` with 400.
  A reading outside its range flags the widget with a HIGH/LOW chip, and
  the detail charts shade the out-of-range region as a faint red zone.

## Wired lane passthrough

- `POST /arduino/command`, body `{"command": "RELAY1:ON"}` — writes one raw
  protocol line to the Arduino (see `serial-protocol.md`). `502` if the
  serial port isn't connected. Exists for any future wired actuator (relay,
  MOSFET dimmer) and manual testing. Ambient lighting (WLED zones, above)
  does **not** use this — it's a WiFi device controlled directly, same lane
  as the myStrom plugs.

## Errors

Failures return `{"error": "<human-readable message>"}` with 400 (bad
params), 404 (unknown device/metric), or 502 (hardware unreachable). The
backend never fakes success — if the plug or serial port is down, callers
hear about it.
