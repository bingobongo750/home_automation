# Firmware — Arduino Due sensor node

Sketch: `hub_node/hub_node.ino`. Reads the wired sensors and speaks the
line-based `KEY:VALUE` serial protocol documented in `/docs/serial-protocol.md`.

**Board note:** the node is an Arduino Due — 3.3V logic, pins **not** 5V
tolerant. The sketch is plain portable Arduino API and still compiles for a
classic Uno if the node is ever swapped back (re-check every signal level
first: the Uno inverts the 3.3V/5V concerns below).

## Required libraries (Arduino Library Manager)

| Library | Used for |
|---|---|
| Adafruit BME280 Library (+ Adafruit Unified Sensor) | temp / humidity |
| BH1750 (by Christopher Laws) | ambient light |
| SparkFun SCD4x Arduino Library | CO2 (SCD40) |

`Wire` is built in. The PIR needs no library.

If you use an SCD30 instead of the SCD40, swap in the SparkFun SCD30 library
and adjust the three `co2Sensor` call sites (`readMeasurement()` becomes
`dataAvailable()`) — the serial output stays identical.

## Wiring / pinout

All I2C breakouts are Adafruit-style boards with onboard regulation, powered
from the Due's **3.3V pin** so the shared I2C bus is pulled up to 3.3V —
never power them from 5V, the Due's pins are not 5V tolerant. The HC-SR501
is the one exception: its regulator needs the **5V pin**, but its output
signal is natively 3.3V and safe to connect directly.

| Device | Pin(s) |
|---|---|
| I2C bus (BME280 0x76/0x77, BH1750 0x23, SCD40 0x62) | SDA = 20, SCL = 21 |
| HC-SR501 PIR output | D2 |
| Relay module IN1 (future — must be a module that triggers at 3.3V) | D7 |
| MOSFET gate (future, PWM dim — needs a 3.3V-gate part; IRLZ44N is marginal at 3.3V) | D9 |
| WS2812B / NeoPixel data (future — 3.3V data usually fine on a short run; else 74AHCT125) | D6 |

Relay, MOSFET, and NeoPixel handlers exist in the sketch as stubs so the
serial protocol surface is complete before the hardware is wired.

## Flashing

Arduino IDE or `arduino-cli` with the **Arduino SAM Boards (32-bits ARM
Cortex-M3)** core installed, board **Arduino Due (Programming Port)**. Use
the programming port (micro-USB nearer the DC jack) — it is `Serial` in the
sketch and the port the host reads. Serial monitor at **115200 baud**. On
boot the sketch prints `#`-prefixed status lines showing which sensors were
detected; missing sensors are skipped, not fatal.

**Free the serial port first.** The hub holds it open and its reconnect loop
reclaims it every 5 s, which makes uploads fail or half-succeed. Release it
without stopping the hub (lighting, scenes, planner and health keep running):

```sh
curl -X POST localhost:8000/api/arduino/serial \
     -H 'Content-Type: application/json' -d '{"paused": true, "minutes": 20}'
```

A `"port_released": true` response means the port is genuinely closed and you
can flash. The pause always self-expires; `{"paused": false}` ends it early.

## Bring-up tools

Throwaway diagnostics, **not** part of the hub. Flash one, fix the sensor,
then flash `hub_node.ino` back.

| Sketch | Purpose |
|---|---|
| `scd40_recovery/` | Interactive raw-I2C SCD40 diagnosis + recovery. Serial menu at 115200: bus scan, self-test, factory reset, guided FRC. **Start here.** |
| `scd40_calibrate/` | Older one-shot FRC via the SparkFun driver. Superseded by the above. |

`scd40_recovery` uses no driver library — only `Wire` — because the driver was
hiding the failure: while periodic measurement is running the sensor **silently
discards** factory-reset and FRC commands, reporting no error, so a driver call
returns success while nothing happened. The sketch tracks measurement state and
refuses restricted commands rather than letting them vanish, and prints every
opcode, `endTransmission()` status, raw byte and decoded value.

It is board-agnostic (`Wire.begin()` picks the right pins): SDA/SCL are 20/21
on the Due, 18/19 on a Teensy 4.1. Compiles clean for both.

Typical order: `0` bus scan → `2` self-test → `1` read config → `4` factory
reset → power-cycle → `1` verify defaults returned → `5` FRC outdoors → `6`
disable ASC if the sensor never sees outdoor air.
