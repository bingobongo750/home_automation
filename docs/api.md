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

- `GET /devices` → array of `{id, name, type, ip, room, power}` where `power`
  is the last polled `{ts, watts, relay_on}` (or `null`) — one call refreshes
  every plug widget.
- `GET /devices/:id` → device row plus `"power": {"ts", "watts", "relay_on"}`
  (last polled sample, `null` if never polled).
- `POST /devices/:id/toggle` → `{"relay_on": true|false}` (new state).
  `502` with `{"error": ...}` if the plug is unreachable.
- `GET /devices/:id/power/history?range=24h` →
  `{"device_id": ..., "points": [{"ts", "watts"}, ...]}` — same range/
  downsampling rules as sensor history.
- `GET /devices/:id/power/stats` → `{"avg_24h_w", "kwh_24h", "avg_7d_w"}` —
  `kwh_24h` is average draw integrated over the hours actually covered by
  samples, so it's an estimate (≈) rather than metered energy.

## Wired lane passthrough

- `POST /arduino/command`, body `{"command": "RELAY1:ON"}` — writes one raw
  protocol line to the Arduino (see `serial-protocol.md`). `502` if the
  serial port isn't connected. This is the seam the future Lighting tab will
  use (`MODE:`, `COLOR:`).

## Errors

Failures return `{"error": "<human-readable message>"}` with 400 (bad
params), 404 (unknown device/metric), or 502 (hardware unreachable). The
backend never fakes success — if the plug or serial port is down, callers
hear about it.
