# Serial protocol — host ⇄ Arduino

Single USB serial connection, **115200 baud**, line-based, human-readable,
`\n`-terminated. This is the seam most likely to drift: if you change anything
here, change it in **both** `/firmware/hub_node/hub_node.ino` and
`/server/app/serial_reader.py` in the same commit.

## Arduino → host (telemetry)

One reading per line, `KEY:VALUE`. Sent every 5 s; `MOTION` is additionally
sent immediately whenever the PIR state changes.

| Line | Meaning | Stored as metric |
|---|---|---|
| `TEMP:21.4` | temperature, °C, 1 decimal | `temp` |
| `HUM:47.2` | relative humidity, %, 1 decimal | `hum` |
| `LUX:312` | ambient light, lux, integer | `lux` |
| `CO2:612` | CO2, ppm, integer | `co2` |
| `MOTION:1` | PIR state, 0/1 | `motion` |

Lines starting with `#` are firmware log/debug output (boot status, command
acks, errors). The host logs them at DEBUG and never stores them.

A sensor that fails to initialize is skipped, not fatal — its key simply never
appears. The host treats unknown keys as protocol drift and logs a warning.

## Host → Arduino (commands)

| Command | Effect (today) |
|---|---|
| `RELAY1:ON` / `RELAY1:OFF` | drives relay pin D7 (module not wired yet) |
| `DIM1:<0-255>` | PWM on pin D9 (MOSFET not wired yet) |
| `MODE:<name>` | NeoPixel mode select — stub, acks with a `#` line |
| `COLOR:r,g,b` | NeoPixel direct color — stub, acks with a `#` line |

The Arduino acks every command with a `#` log line, and answers malformed or
unknown commands with `# ERR ...`. Commands can be sent from the host via
`POST /api/arduino/command` with body `{"command": "RELAY1:ON"}`.

## Design rules

- `KEY:VALUE` framing, never JSON — the Uno has 2 KB of RAM.
- Never send per-pixel / per-frame data over serial; NeoPixel animation logic
  belongs on the Arduino, selected by short `MODE:` commands.
- Keep values short and numeric so host parsing stays a `partition(":")`.
