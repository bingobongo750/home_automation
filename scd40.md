# SCD40 Diagnostic & Recalibration Plan (Teensy 4.1 / Arduino)

## Task for Claude Code

Write a **single self-contained Arduino sketch** (`scd40_recovery.ino`) that runs an interactive, serial-menu-driven diagnostic and recalibration procedure for a Sensirion SCD40 CO₂ sensor. Do **not** use the Sensirion or Adafruit driver libraries — use raw `Wire` I²C so that every command, delay, and return value is explicit and inspectable. The whole point of this exercise is that library abstractions have been hiding the failure.

### Symptom being diagnosed

Ambient CO₂ reads exactly `0` ppm. Breathing on the sensor produces plausible, responsive values. Temperature and humidity appear normal. Previous recalibration and factory-reset attempts had no effect.

### Working hypothesis

CO₂ is reported as an **unsigned** 16-bit word. The sensor computes `raw − baseline`. A corrupted (too-high) baseline drives the result negative, which clamps to `0`. Exhaled breath (~40 000 ppm) exceeds the bad baseline, so readings reappear. This is a recoverable calibration fault, not silicon damage.

The previous reset attempts most likely failed silently because `perform_factory_reset` is **ignored without error** if the sensor is in periodic measurement mode, and because the mandatory post-command delays were not observed.

---

## 1. Hardware & bus constraints

| Item | Value | Note |
|---|---|---|
| I²C address | `0x62` | 7-bit, not configurable |
| Max I²C clock | **100 kHz** | SCD4x is *not* 400 kHz rated. Call `Wire.setClock(100000)` explicitly. |
| Supply | 3.3 V | Teensy 4.1 logic is 3.3 V — direct connection, no level shifter |
| Teensy 4.1 I²C0 | SDA = pin 18, SCL = pin 19 | |
| Peak current | ~205 mA during measurement | See power note below |
| Power-up delay | 1000 ms after VDD stable before first command | |

**Power note to verify before blaming the sensor:** the SCD4x draws ~205 mA current spikes every measurement cycle. If it shares the Teensy's 3.3 V regulator with the ADXL355 and LSM6DSOX, brownout during conversion is plausible. Add 100 µF bulk + 100 nF decoupling physically close to the sensor, and if the rail looks marginal, power the SCD40 from a separate 3.3 V source with common ground.

**Clock stretching:** the SCD4x does not use clock stretching for these commands. All timing is handled by fixed host-side delays. Every delay below is mandatory, not advisory.

---

## 2. Command reference (implement as named constants)

| Command | Opcode | Post-command delay | Response |
|---|---|---|---|
| `start_periodic_measurement` | `0x21B1` | — | none |
| `stop_periodic_measurement` | `0x3F86` | **500 ms** | none |
| `read_measurement` | `0xEC05` | 1 ms | 9 bytes (3 words) |
| `get_data_ready_status` | `0xE4B8` | 1 ms | 3 bytes (1 word) |
| `perform_forced_recalibration` | `0x362F` | **400 ms** | 3 bytes (1 word) |
| `get_automatic_self_calibration_enabled` | `0x2313` | 1 ms | 3 bytes |
| `set_automatic_self_calibration_enabled` | `0x2416` | 1 ms | none |
| `get_temperature_offset` | `0x2318` | 1 ms | 3 bytes |
| `set_temperature_offset` | `0x241D` | 1 ms | none |
| `get_sensor_altitude` | `0x2322` | 1 ms | 3 bytes |
| `set_sensor_altitude` | `0x2427` | 1 ms | none |
| `set_ambient_pressure` | `0xE000` | 1 ms | none |
| `persist_settings` | `0x3615` | **800 ms** | none |
| `get_serial_number` | `0x3682` | 1 ms | 9 bytes (3 words) |
| `perform_self_test` | `0x3639` | **10 000 ms** | 3 bytes (1 word) |
| `perform_factory_reset` | `0x3632` | **1200 ms** | none |
| `reinit` | `0x3646` | 30 ms | none |

**Critical rule:** while periodic measurement is running, the *only* legal commands are `read_measurement`, `get_data_ready_status`, `stop_periodic_measurement`, and `set_ambient_pressure`. Everything else — including factory reset and FRC — is silently discarded. The sketch must track measurement state in a global `bool g_measuring` and refuse to issue restricted commands when it is true.

Cross-check these opcodes and delays against the Sensirion SCD4x datasheet revision that matches the part before trusting them; values have shifted slightly between revisions.

---

## 3. Data conversion

- **CRC-8:** polynomial `0x31`, init `0xFF`, no final XOR, MSB-first, computed over each 2-byte word.
- **CO₂:** word value directly in ppm.
- **Temperature:** `T_degC = -45.0 + 175.0 * word / 65536.0`
- **Humidity:** `RH_pct = 100.0 * word / 65536.0`
- **FRC return:** `correction_ppm = (int32_t)word - 0x8000`. A raw value of `0xFFFF` means **FRC failed**.
- **Self-test return:** `0x0000` = no malfunction detected. Anything else = hardware fault.
- **Temperature offset word:** `offset_degC = 175.0 * word / 65536.0`, inverse for setting.

---

## 4. Required low-level functions

```cpp
#include <Wire.h>

static const uint8_t SCD4X_ADDR = 0x62;
static bool g_measuring = false;

uint8_t crc8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0xFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

// Returns Wire.endTransmission() status: 0 = OK.
uint8_t sendCmd(uint16_t cmd) {
  Wire.beginTransmission(SCD4X_ADDR);
  Wire.write((uint8_t)(cmd >> 8));
  Wire.write((uint8_t)(cmd & 0xFF));
  return Wire.endTransmission();
}

uint8_t sendCmdArg(uint16_t cmd, uint16_t arg) {
  uint8_t a[2] = { (uint8_t)(arg >> 8), (uint8_t)(arg & 0xFF) };
  Wire.beginTransmission(SCD4X_ADDR);
  Wire.write((uint8_t)(cmd >> 8));
  Wire.write((uint8_t)(cmd & 0xFF));
  Wire.write(a[0]);
  Wire.write(a[1]);
  Wire.write(crc8(a, 2));
  return Wire.endTransmission();
}

// Reads n words, verifying CRC on each. Returns false on short read or CRC error.
bool readWords(uint16_t *out, uint8_t n) {
  const uint8_t need = n * 3;
  if (Wire.requestFrom(SCD4X_ADDR, need) != need) return false;
  for (uint8_t i = 0; i < n; i++) {
    uint8_t msb = Wire.read(), lsb = Wire.read(), crc = Wire.read();
    uint8_t d[2] = { msb, lsb };
    if (crc8(d, 2) != crc) return false;
    out[i] = ((uint16_t)msb << 8) | lsb;
  }
  return true;
}
```

**Every** command wrapper must print its opcode, the `endTransmission()` status, the raw bytes read, and the decoded value to Serial. Silent success is what caused this problem in the first place — the sketch should make it impossible to not notice a failure.

---

## 5. Serial menu structure

`setup()`: `Serial.begin(115200)`, wait for USB serial (with a ~5 s timeout so it still runs headless), `Wire.begin()`, `Wire.setClock(100000)`, `delay(1000)` for sensor power-up, then print the menu.

Menu options, each a single keystroke:

```
[0] Bus scan + serial number      (identity / wiring check)
[1] Read full sensor state        (ASC, T-offset, altitude)
[2] Self-test                     (10 s — hardware verdict)
[3] Continuous measurement        (any key stops)
[4] Clean factory reset           (full correct sequence)
[5] Forced recalibration (FRC)    (guided, 5 min warm-up)
[6] Disable ASC + persist
[7] Enable ASC + persist
[8] Set sensor altitude + persist
```

---

## 6. Phase-by-phase procedure

### Phase 0 — Identity and bus integrity (menu `0`)

1. I²C bus scan across `0x08`–`0x77`; confirm exactly one device responds at `0x62`. If nothing responds, stop — this is a wiring, pull-up, or power problem, not a calibration problem.
2. `get_serial_number` (`0x3682`, 1 ms) → 3 words → concatenate into a 48-bit ID, print as hex. A stable, non-zero, non-`0xFFFF` serial across repeated reads proves clean I²C signalling.

**Fail condition:** CRC errors or a serial that changes between reads → bus integrity problem (pull-ups, cable length, clock too fast). Fix before proceeding.

### Phase 1 — Read current configuration (menu `1`)

Must be run with measurement stopped. Read and print:
- `get_automatic_self_calibration_enabled` → 1 = ASC on
- `get_temperature_offset` → decode to °C (factory default 4.0 °C)
- `get_sensor_altitude` → metres (factory default 0)

**This is diagnostic evidence.** If ASC reads `1` and the sensor has been living in an enclosure or a room that never reaches outdoor CO₂ levels, that is almost certainly the root cause: ASC takes the minimum concentration seen over its calibration window and asserts it equals ~400 ppm. A week of never dropping below 1500 ppm produces roughly an 1100 ppm negative offset — enough to clamp every ambient reading to zero.

### Phase 2 — Self-test: the hardware verdict (menu `2`)

1. `stop_periodic_measurement` → **500 ms**
2. `perform_self_test` (`0x3639`) → **10 000 ms** → read 1 word

- `0x0000` → **no malfunction detected. The part is good — proceed with recovery.**
- anything else → genuine hardware fault; the sensor is defective and should be replaced.

Print the raw word either way. Do not swallow it into a boolean.

### Phase 3 — Clean factory reset (menu `4`)

Executed in exactly this order, with these delays. This is what most likely went wrong before.

1. `stop_periodic_measurement` → **`delay(500)`** ← omitting this invalidates everything after it
2. Set `g_measuring = false`
3. `perform_factory_reset` → **`delay(1200)`**
4. `reinit` → `delay(30)`
5. Print a prompt instructing the user to **physically power-cycle the board now**, then halt the menu until a key is pressed.
6. After the power cycle, re-run Phase 1 and confirm the settings have returned to factory defaults (ASC = 1, T-offset ≈ 4.0 °C, altitude = 0). **If they have not changed, the reset did not take** — check that `endTransmission()` returned 0 at every step and that no measurement was running.

### Phase 4 — Forced recalibration against real fresh air (menu `5`)

Interactive, and it must refuse to proceed if the user skips a step.

**Environment:** genuine outdoor air. A balcony or wide-open window with air movement works; a "well-ventilated room" does not. Place the sensor and stand well away from it — a human at arm's length is a significant CO₂ source. Use a target of **425 ppm**, not 400 — that is roughly the current global outdoor background, and using the stale 400 figure bakes in a systematic error.

Sequence:
1. Prompt: "Place sensor in outdoor air, move away, press any key."
2. `start_periodic_measurement` (`0x21B1`), set `g_measuring = true`
3. Run for **at least 3 minutes — use 5 for margin.** During this time, poll `get_data_ready_status` every 5 s and print each reading with a countdown. The readings will likely still show 0 — that is expected and not a reason to abort. The sensor needs this window to stabilise its raw signal; FRC computed on an unstabilised sensor produces a garbage correction.
4. `stop_periodic_measurement` → **`delay(500)`**, `g_measuring = false`
5. `perform_forced_recalibration` with argument `425` → **`delay(400)`** → read 1 word

**Interpret the return value — this is the single most important output of the whole procedure:**

- Raw `0xFFFF` → **FRC FAILED.** The sensor was in the wrong state. Print an explicit failure banner. Do not report success. Re-check that measurement was actually stopped and the 500 ms delay elapsed.
- Otherwise → `correction_ppm = (int32_t)word - 0x8000`. Print it prominently. A large negative correction (order −800 to −1500 ppm) directly confirms the drifted-baseline hypothesis and tells you exactly how far off it was.

6. Restart periodic measurement and stream readings for 2 minutes so the user can confirm ambient values are now plausible (indoors: ~400–1200 ppm depending on ventilation and occupancy).

### Phase 5 — Lock in the configuration (menu `6` / `8`)

**Disable ASC** if the sensor lives in an enclosure, a sealed instrument, or any environment that does not reliably see outdoor air at some point every few days. Otherwise ASC will silently re-corrupt the baseline within about a week and you will be back where you started.

1. `set_automatic_self_calibration_enabled` with argument `0`
2. `persist_settings` (`0x3615`) → **`delay(800)`**
3. Read back with `get_automatic_self_calibration_enabled` to confirm `0`

**Optionally set altitude** (`set_sensor_altitude`, argument in metres above sea level) before persisting, so pressure compensation is correct for the deployment site. Persist once with both settings applied rather than calling `persist_settings` twice.

**EEPROM warning to put in a code comment:** `persist_settings` writes to an EEPROM rated for roughly 2000 cycles. Call it only when a setting has actually changed. Never put it inside a loop or a startup routine that runs on every boot.

With ASC disabled, plan a manual FRC every few months, or whenever the readings look suspect.

---

## 7. Acceptance criteria

The sketch is done when all of the following hold:

1. Bus scan finds exactly one device at `0x62`; serial number is stable across 10 consecutive reads.
2. `perform_self_test` returns `0x0000`.
3. After factory reset and power cycle, Phase 1 shows factory-default settings — proving the reset actually took effect this time.
4. FRC returns a value other than `0xFFFF`, and the decoded correction is printed in ppm.
5. Ambient indoor readings are non-zero and respond sensibly: they rise when someone is near the sensor and decay over minutes when a window is opened.
6. Every I²C transaction's `endTransmission()` status is checked and printed; no failure path is silent.

If criterion 2 fails, the part is genuinely defective. If criterion 2 passes but criterion 4 keeps failing with `0xFFFF`, the problem is command sequencing or timing in the sketch, not the sensor.

---

## 8. Style requirements

- Single `.ino` file, no external dependencies beyond `Wire.h`.
- All opcodes as named `static const uint16_t` constants, not magic numbers.
- All mandatory delays as named constants with a comment giving the datasheet-specified minimum.
- Non-blocking where reasonable, but the mandatory delays are blocking by design — do not "optimise" them away with `millis()` polling that could shorten them.
- Verbose serial output throughout: opcode, transaction status, raw bytes, decoded value. Verbosity here is the feature.