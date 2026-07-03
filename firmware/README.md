# Firmware — Arduino Uno sensor node

Sketch: `hub_node/hub_node.ino`. Reads the wired sensors and speaks the
line-based `KEY:VALUE` serial protocol documented in `/docs/serial-protocol.md`.

## Required libraries (Arduino Library Manager)

| Library | Used for |
|---|---|
| Adafruit BME280 Library (+ Adafruit Unified Sensor) | temp / humidity |
| BH1750 (by Christopher Laws) | ambient light |
| SparkFun SCD30 Arduino Library | CO2 |

`Wire` is built in. The PIR needs no library.

If you use an SCD40/SCD41 instead of the SCD30, swap in Sensirion's
`Sensirion I2C SCD4x` library and adjust the three `co2Sensor` call sites —
the serial output stays identical.

## Wiring / pinout

All breakouts are Adafruit-style boards with onboard regulation, powered from
the Uno's 5V rail via breadboard.

| Device | Pin(s) |
|---|---|
| I2C bus (BME280 0x76/0x77, BH1750 0x23, SCD30 0x61) | SDA = A4, SCL = A5 |
| HC-SR501 PIR output | D2 |
| Relay module IN1 (future) | D7 |
| MOSFET gate, IRLZ44N (future, PWM dim) | D9 |
| WS2812B / NeoPixel data (future) | D6 |

Relay, MOSFET, and NeoPixel handlers exist in the sketch as stubs so the
serial protocol surface is complete before the hardware is wired.

## Flashing

Arduino IDE or `arduino-cli`, board **Arduino Uno**, serial monitor at
**115200 baud**. On boot the sketch prints `#`-prefixed status lines showing
which sensors were detected; missing sensors are skipped, not fatal.
