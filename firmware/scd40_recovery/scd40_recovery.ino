/*
 * scd40_recovery.ino — interactive SCD40 diagnostic & recalibration tool.
 *
 * Throwaway bring-up tool, NOT part of the hub. Flash it, recover the sensor,
 * then flash hub_node.ino back. Supersedes scd40_calibrate.ino, which drove the
 * same sequence through the SparkFun driver — this one talks raw Wire so every
 * command, delay and return value is explicit and inspectable.
 *
 * WHY RAW I2C: the library hid the failure. perform_factory_reset and FRC are
 * silently DISCARDED while periodic measurement is running — no error, no
 * exception, the driver reports success and nothing changed. That is the most
 * likely reason previous reset attempts had no effect. Here, every transaction
 * prints its opcode, its endTransmission() status, its raw bytes and its
 * decoded value, and a g_measuring guard refuses restricted commands outright.
 *
 * SYMPTOM: ambient CO2 reads exactly 0 ppm while breath produces plausible,
 * responsive values, and T/RH look normal. CO2 is reported as an UNSIGNED
 * 16-bit word of (raw - baseline); a corrupted too-high baseline drives that
 * negative and it clamps at 0. Exhaled breath (~40000 ppm) clears the bad
 * baseline, so readings reappear. Recoverable calibration fault, not damage.
 *
 * WIRING: SCD40 on the default I2C bus at 0x62, powered from 3.3V.
 *   Arduino Due  — SDA = 20, SCL = 21   (this project's hub board)
 *   Teensy 4.1   — SDA = 18, SCL = 19
 * Wire.begin() picks the right pins on both, so nothing here is board-specific.
 *
 * POWER: the SCD4x pulls ~205 mA spikes every measurement cycle. If ambient
 * readings are erratic rather than pinned at zero, suspect the rail before the
 * sensor — add 100 uF bulk + 100 nF decoupling physically close to the part,
 * and if it shares a regulator with other hungry peripherals, give it its own
 * 3.3V supply with a common ground.
 *
 * USAGE: open the serial monitor at 115200 and press a menu key. Start with 0
 * (is the bus sane?), then 2 (is the part good?), then 4 and 5 to recover it.
 */

#include <Wire.h>

static const uint8_t SCD4X_ADDR = 0x62;   // 7-bit, not configurable

// The SCD4x is NOT 400 kHz rated. Set this explicitly — a core that defaults
// to 400 kHz will produce intermittent CRC failures that look like a dead part.
static const uint32_t I2C_CLOCK_HZ = 100000;

// ---------------------------------------------------------------- opcodes
// Cross-check against the datasheet revision matching your part before
// trusting these; a few have shifted slightly between revisions.
static const uint16_t CMD_START_PERIODIC   = 0x21B1;
static const uint16_t CMD_STOP_PERIODIC    = 0x3F86;
static const uint16_t CMD_READ_MEASUREMENT = 0xEC05;
static const uint16_t CMD_DATA_READY       = 0xE4B8;
static const uint16_t CMD_FRC              = 0x362F;
static const uint16_t CMD_GET_ASC          = 0x2313;
static const uint16_t CMD_SET_ASC          = 0x2416;
static const uint16_t CMD_GET_TEMP_OFFSET  = 0x2318;
static const uint16_t CMD_SET_TEMP_OFFSET  = 0x241D;
static const uint16_t CMD_GET_ALTITUDE     = 0x2322;
static const uint16_t CMD_SET_ALTITUDE     = 0x2427;
static const uint16_t CMD_SET_AMB_PRESSURE = 0xE000;
static const uint16_t CMD_PERSIST_SETTINGS = 0x3615;
static const uint16_t CMD_GET_SERIAL       = 0x3682;
static const uint16_t CMD_SELF_TEST        = 0x3639;
static const uint16_t CMD_FACTORY_RESET    = 0x3632;
static const uint16_t CMD_REINIT           = 0x3646;

// ----------------------------------------------------------------- delays
// Datasheet-specified minimums. These are mandatory, not advisory: the SCD4x
// does not clock-stretch for these commands, so the host is solely responsible
// for the timing. Do NOT replace them with millis() polling that could shorten
// them — a short delay here is exactly the bug this sketch exists to rule out.
static const uint32_t D_POWER_UP      = 1000;   // after VDD stable
static const uint32_t D_STOP_PERIODIC = 500;    // omitting this voids what follows
static const uint32_t D_SHORT         = 1;      // ordinary read commands
static const uint32_t D_FRC           = 400;
static const uint32_t D_PERSIST       = 800;
static const uint32_t D_SELF_TEST     = 10000;
static const uint32_t D_FACTORY_RESET = 1200;
static const uint32_t D_REINIT        = 30;

// --------------------------------------------------------------- settings
// 425 ppm, not the stale 400: that is roughly the current global outdoor
// background, and calibrating to 400 bakes in a systematic ~25 ppm error.
static const uint16_t FRC_TARGET_PPM = 425;

// Sensirion's minimum is 3 minutes of periodic measurement in the reference
// air; 5 gives margin. FRC computed on an unstabilised sensor returns garbage.
static const uint32_t FRC_WARMUP_MS = 5UL * 60UL * 1000UL;

static bool g_measuring = false;   // mirrors the sensor's periodic-measurement state

// ------------------------------------------------------------ print helpers

static void printHex8(uint8_t v) {
  if (v < 0x10) Serial.print('0');
  Serial.print(v, HEX);
}

static void printHex16(uint16_t v) {
  if (v < 0x1000) Serial.print('0');
  if (v < 0x100)  Serial.print('0');
  if (v < 0x10)   Serial.print('0');
  Serial.print(v, HEX);
}

static void rule() {
  Serial.println(F("--------------------------------------------------------"));
}

// Decodes the endTransmission() status rather than printing a bare number —
// 2 (address NACK) and 3 (data NACK) mean completely different things.
static void printTxStatus(uint8_t st) {
  Serial.print(F("   endTransmission = "));
  Serial.print(st);
  switch (st) {
    case 0: Serial.println(F(" (OK)")); break;
    case 1: Serial.println(F(" (FAIL: data too long for buffer)")); break;
    case 2: Serial.println(F(" (FAIL: address NACK — nothing at 0x62?)")); break;
    case 3: Serial.println(F(" (FAIL: data NACK)")); break;
    case 4: Serial.println(F(" (FAIL: other bus error)")); break;
    default: Serial.println(F(" (FAIL: unknown)")); break;
  }
}

// ------------------------------------------------------------- I2C plumbing

// CRC-8: poly 0x31, init 0xFF, no final XOR, MSB-first, over each 2-byte word.
static uint8_t crc8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0xFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

static uint8_t sendCmd(uint16_t cmd, const __FlashStringHelper *name) {
  Serial.print(F("-> cmd 0x"));
  printHex16(cmd);
  Serial.print(F("  "));
  Serial.println(name);
  Wire.beginTransmission(SCD4X_ADDR);
  Wire.write((uint8_t)(cmd >> 8));
  Wire.write((uint8_t)(cmd & 0xFF));
  uint8_t st = Wire.endTransmission();
  printTxStatus(st);
  return st;
}

static uint8_t sendCmdArg(uint16_t cmd, uint16_t arg, const __FlashStringHelper *name) {
  uint8_t a[2] = { (uint8_t)(arg >> 8), (uint8_t)(arg & 0xFF) };
  Serial.print(F("-> cmd 0x"));
  printHex16(cmd);
  Serial.print(F("  "));
  Serial.print(name);
  Serial.print(F("  arg=0x"));
  printHex16(arg);
  Serial.print(F(" ("));
  Serial.print(arg);
  Serial.println(F(")"));
  Wire.beginTransmission(SCD4X_ADDR);
  Wire.write((uint8_t)(cmd >> 8));
  Wire.write((uint8_t)(cmd & 0xFF));
  Wire.write(a[0]);
  Wire.write(a[1]);
  Wire.write(crc8(a, 2));
  uint8_t st = Wire.endTransmission();
  printTxStatus(st);
  return st;
}

// Reads n words (max 3), verifying CRC on each. Prints the raw bytes either
// way — a CRC failure with plausible-looking bytes is a bus problem, and you
// cannot tell that from a bare false.
static bool readWords(uint16_t *out, uint8_t n) {
  const uint8_t need = (uint8_t)(n * 3);
  uint8_t got = Wire.requestFrom((uint8_t)SCD4X_ADDR, need);
  if (got != need) {
    Serial.print(F("<- SHORT READ: asked "));
    Serial.print(need);
    Serial.print(F(" bytes, got "));
    Serial.println(got);
    while (Wire.available()) Wire.read();   // leave the bus clean
    return false;
  }

  uint8_t raw[9];
  for (uint8_t i = 0; i < need; i++) raw[i] = Wire.read();

  Serial.print(F("<- raw:"));
  for (uint8_t i = 0; i < need; i++) {
    Serial.print(' ');
    printHex8(raw[i]);
    if (i % 3 == 2) Serial.print(' ');
  }
  Serial.println();

  bool ok = true;
  for (uint8_t i = 0; i < n; i++) {
    uint8_t d[2] = { raw[i * 3], raw[i * 3 + 1] };
    uint8_t crc = raw[i * 3 + 2];
    out[i] = ((uint16_t)d[0] << 8) | d[1];
    if (crc8(d, 2) != crc) {
      Serial.print(F("   !! CRC FAIL on word "));
      Serial.print(i);
      Serial.print(F(": got 0x"));
      printHex8(crc);
      Serial.print(F(", expected 0x"));
      printHex8(crc8(d, 2));
      Serial.println();
      ok = false;
    }
  }

  Serial.print(F("   words:"));
  for (uint8_t i = 0; i < n; i++) {
    Serial.print(F(" 0x"));
    printHex16(out[i]);
  }
  Serial.println();
  return ok;
}

// The guard that the driver never had. While periodic measurement runs, the
// ONLY legal commands are read_measurement, get_data_ready_status,
// stop_periodic_measurement and set_ambient_pressure — everything else is
// silently discarded by the sensor.
static bool requireIdle(const __FlashStringHelper *what) {
  if (!g_measuring) return true;
  Serial.print(F("!! REFUSED: "));
  Serial.print(what);
  Serial.println(F(" needs periodic measurement STOPPED."));
  Serial.println(F("!! The sensor would discard it silently and report nothing."));
  Serial.println(F("!! Run [4] or stop measurement first."));
  return false;
}

// ------------------------------------------------------------ decode helpers

static float decodeTemp(uint16_t w)     { return -45.0f + 175.0f * (float)w / 65536.0f; }
static float decodeHumidity(uint16_t w) { return 100.0f * (float)w / 65536.0f; }
static float decodeTempOffset(uint16_t w) { return 175.0f * (float)w / 65536.0f; }

// The write side of the T-offset conversion, kept because CMD_SET_TEMP_OFFSET
// exists and a future menu entry will need it. Nothing calls it today: the
// menu deliberately exposes no T-offset setter, since a wrong offset skews RH
// and CO2 compensation and is not part of recovering a clamped baseline.
static uint16_t encodeTempOffset(float c) __attribute__((unused));
static uint16_t encodeTempOffset(float c) { return (uint16_t)(c * 65536.0f / 175.0f); }

// Only bits 10:0 carry the ready flag; the upper bits are undefined and MUST
// be masked off. Comparing the whole word to zero gives a sensor that never
// looks ready.
static bool dataReady(uint16_t w) { return (w & 0x07FF) != 0; }

// ------------------------------------------------------------- serial input

static void flushInput() {
  while (Serial.available()) Serial.read();
}

static int readKeyBlocking() {
  for (;;) {
    while (!Serial.available()) { /* wait */ }
    int c = Serial.read();
    if (c != '\r' && c != '\n') return c;
  }
}

static void waitAnyKey(const __FlashStringHelper *prompt) {
  Serial.println(prompt);
  flushInput();
  readKeyBlocking();
}

// Reads a decimal number terminated by newline, echoing as it goes. Returns
// READ_CANCELLED on an empty line; callers pass non-negative ranges so the
// sentinel can never collide with a real answer.
static const long READ_CANCELLED = -1;

static long readNumber(const __FlashStringHelper *prompt, long lo, long hi) {
  for (;;) {
    Serial.print(prompt);
    Serial.print(F(" ["));
    Serial.print(lo);
    Serial.print(F(".."));
    Serial.print(hi);
    Serial.print(F("]: "));
    flushInput();

    String buf;
    for (;;) {
      while (!Serial.available()) { /* wait */ }
      int c = Serial.read();
      if (c == '\r') continue;
      if (c == '\n') break;
      if (c == 8 || c == 127) {                  // backspace
        if (buf.length()) { buf.remove(buf.length() - 1); Serial.print(F("\b \b")); }
        continue;
      }
      buf += (char)c;
      Serial.print((char)c);
    }
    Serial.println();

    buf.trim();
    if (buf.length() == 0) { Serial.println(F("   (cancelled)")); return READ_CANCELLED; }
    long v = buf.toInt();
    if (v == 0 && buf[0] != '0') { Serial.println(F("   not a number, try again")); continue; }
    if (v < lo || v > hi) { Serial.println(F("   out of range, try again")); continue; }
    return v;
  }
}

// ============================================================ Phase 0: bus

static void phaseBusScan() {
  rule();
  Serial.println(F("PHASE 0 — bus scan + serial number"));
  rule();

  uint8_t found = 0;
  bool sawScd = false;
  Serial.println(F("Scanning 0x08..0x77 ..."));
  for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.print(F("  device at 0x"));
      printHex8(addr);
      if (addr == SCD4X_ADDR) { Serial.print(F("  <- SCD4x")); sawScd = true; }
      Serial.println();
      found++;
    }
  }
  Serial.print(F("Devices found: "));
  Serial.println(found);

  if (found == 0) {
    Serial.println(F("!! NOTHING ON THE BUS. This is a wiring, pull-up or power"));
    Serial.println(F("!! fault, not a calibration fault. Stop here and fix it."));
    return;
  }
  if (!sawScd) {
    Serial.println(F("!! The bus works, but NOTHING ANSWERS AT 0x62. The SCD40 is"));
    Serial.println(F("!! not connected, not powered, or dead. No calibration step"));
    Serial.println(F("!! can help until this address responds."));
    return;
  }
  // More than one device is normal and fine — this bus is shared (BME280,
  // BH1750 on the hub; ADXL355, LSM6DSOX on the Teensy rig). Only 0x62 matters
  // here, and a conflict is impossible since the SCD4x address is fixed.

  // Identity registers only answer in idle mode.
  if (!requireIdle(F("get_serial_number"))) return;

  Serial.println();
  Serial.println(F("Reading serial number 10x — it must be identical every time."));
  uint16_t first[3] = { 0, 0, 0 };
  bool stable = true, allRead = true;

  for (uint8_t attempt = 0; attempt < 10; attempt++) {
    Serial.print(F("["));
    Serial.print(attempt + 1);
    Serial.println(F("/10]"));
    if (sendCmd(CMD_GET_SERIAL, F("get_serial_number")) != 0) { allRead = false; break; }
    delay(D_SHORT);
    uint16_t w[3];
    if (!readWords(w, 3)) { allRead = false; break; }

    Serial.print(F("   serial = 0x"));
    printHex16(w[0]); printHex16(w[1]); printHex16(w[2]);
    Serial.println();

    if (attempt == 0) {
      memcpy(first, w, sizeof(first));
    } else if (memcmp(first, w, sizeof(first)) != 0) {
      stable = false;
    }
    delay(20);
  }

  Serial.println();
  bool degenerate = (first[0] == 0 && first[1] == 0 && first[2] == 0) ||
                    (first[0] == 0xFFFF && first[1] == 0xFFFF && first[2] == 0xFFFF);

  if (allRead && stable && !degenerate) {
    Serial.println(F(">> PASS: serial stable and plausible. I2C signalling is clean."));
  } else {
    Serial.println(F("!! FAIL: bus integrity problem — CRC errors, a changing serial,"));
    Serial.println(F("!! or an all-0000/all-FFFF ID. Check pull-ups, cable length and"));
    Serial.println(F("!! that the clock really is 100 kHz. Fix before going further."));
  }
}

// ====================================================== Phase 1: config read

static void phaseReadConfig() {
  rule();
  Serial.println(F("PHASE 1 — current configuration"));
  rule();
  if (!requireIdle(F("configuration reads"))) return;

  uint16_t w;

  if (sendCmd(CMD_GET_ASC, F("get_automatic_self_calibration_enabled")) == 0) {
    delay(D_SHORT);
    if (readWords(&w, 1)) {
      Serial.print(F("   ASC = "));
      Serial.print(w);
      Serial.println(w ? F("  (ENABLED)") : F("  (disabled)"));
      if (w) {
        Serial.println();
        Serial.println(F("   >> DIAGNOSTIC EVIDENCE. ASC assumes the sensor regularly"));
        Serial.println(F("   >> sees outdoor air and treats the lowest concentration in"));
        Serial.println(F("   >> its window as ~400 ppm. In a room that never drops below"));
        Serial.println(F("   >> ~1500 ppm this stores an ~1100 ppm negative offset —"));
        Serial.println(F("   >> enough to clamp every ambient reading to exactly 0."));
        Serial.println(F("   >> If that describes this sensor, ASC is the root cause."));
      }
    }
  }

  Serial.println();
  if (sendCmd(CMD_GET_TEMP_OFFSET, F("get_temperature_offset")) == 0) {
    delay(D_SHORT);
    if (readWords(&w, 1)) {
      Serial.print(F("   T-offset = "));
      Serial.print(decodeTempOffset(w), 2);
      Serial.println(F(" C   (factory default 4.00)"));
    }
  }

  Serial.println();
  if (sendCmd(CMD_GET_ALTITUDE, F("get_sensor_altitude")) == 0) {
    delay(D_SHORT);
    if (readWords(&w, 1)) {
      Serial.print(F("   altitude = "));
      Serial.print(w);
      Serial.println(F(" m   (factory default 0)"));
    }
  }
}

// ======================================================== Phase 2: self-test

static void phaseSelfTest() {
  rule();
  Serial.println(F("PHASE 2 — self-test (the hardware verdict)"));
  rule();

  if (g_measuring) {
    if (sendCmd(CMD_STOP_PERIODIC, F("stop_periodic_measurement")) != 0) return;
    delay(D_STOP_PERIODIC);
    g_measuring = false;
  }

  Serial.println(F("Running self-test — this takes 10 seconds, do not interrupt."));
  if (sendCmd(CMD_SELF_TEST, F("perform_self_test")) != 0) return;
  delay(D_SELF_TEST);

  uint16_t w;
  if (!readWords(&w, 1)) {
    Serial.println(F("!! could not read the self-test result"));
    return;
  }

  Serial.println();
  Serial.print(F(">> self-test raw = 0x"));
  printHex16(w);
  Serial.println();
  if (w == 0x0000) {
    Serial.println(F(">> PASS: no malfunction detected. The part is GOOD —"));
    Serial.println(F(">> proceed with recovery ([4] then [5])."));
  } else {
    Serial.println(F("!! FAIL: genuine hardware fault. This sensor is defective"));
    Serial.println(F("!! and should be replaced — no amount of recalibration will"));
    Serial.println(F("!! fix it."));
  }
}

// =================================================== Phase 3: continuous read

static void phaseContinuous() {
  rule();
  Serial.println(F("PHASE 3 — continuous measurement (press any key to stop)"));
  rule();

  if (!g_measuring) {
    if (sendCmd(CMD_START_PERIODIC, F("start_periodic_measurement")) != 0) return;
    g_measuring = true;
  }
  Serial.println(F("Sensor self-paces at ~5 s. Waiting for samples..."));
  flushInput();

  while (!Serial.available()) {
    uint16_t w;
    if (sendCmd(CMD_DATA_READY, F("get_data_ready_status")) != 0) break;
    delay(D_SHORT);
    if (!readWords(&w, 1)) break;

    if (!dataReady(w)) {
      Serial.println(F("   not ready yet"));
      delay(1000);
      continue;
    }

    if (sendCmd(CMD_READ_MEASUREMENT, F("read_measurement")) != 0) break;
    delay(D_SHORT);
    uint16_t m[3];
    if (!readWords(m, 3)) break;

    Serial.print(F("   >> CO2 = "));
    Serial.print(m[0]);
    Serial.print(F(" ppm    T = "));
    Serial.print(decodeTemp(m[1]), 1);
    Serial.print(F(" C    RH = "));
    Serial.print(decodeHumidity(m[2]), 0);
    Serial.println(F(" %"));

    if (m[0] == 0) {
      // The same 9-byte frame carries T and RH, which separates the two faults:
      // an all-zero frame means the sensor produced no measurement at all,
      // whereas plausible T/RH alongside co2=0 means only the CO2 channel is
      // clamped — the recoverable baseline case.
      if (m[1] == 0 && m[2] == 0) {
        Serial.println(F("   !! all-zero frame — sensor produced NO measurement."));
        Serial.println(F("   !! Wiring/power fault. FRC will not help. Run [0] and [2]."));
      } else {
        Serial.println(F("   !! co2=0 with plausible T/RH — CO2 channel clamped."));
        Serial.println(F("   !! This is the recoverable baseline case. Run [4] then [5]."));
      }
    }
    delay(1000);
  }

  flushInput();
  Serial.println(F("Stopped streaming (sensor left in periodic measurement)."));
}

// ==================================================== Phase 4: factory reset

static void phaseFactoryReset() {
  rule();
  Serial.println(F("PHASE 4 — clean factory reset"));
  rule();
  Serial.println(F("Exact order and delays matter. This is what most likely went"));
  Serial.println(F("wrong on previous attempts."));
  Serial.println();

  // Step 1-2: stop measurement, and only then claim we are idle.
  Serial.println(F("[1/4] stop periodic measurement"));
  if (sendCmd(CMD_STOP_PERIODIC, F("stop_periodic_measurement")) != 0) {
    Serial.println(F("!! stop failed — aborting before it can corrupt anything."));
    return;
  }
  delay(D_STOP_PERIODIC);
  g_measuring = false;

  Serial.println();
  Serial.println(F("[2/4] factory reset"));
  if (sendCmd(CMD_FACTORY_RESET, F("perform_factory_reset")) != 0) {
    Serial.println(F("!! factory reset NACKed — it did not happen."));
    return;
  }
  delay(D_FACTORY_RESET);

  Serial.println();
  Serial.println(F("[3/4] reinit"));
  if (sendCmd(CMD_REINIT, F("reinit")) != 0) {
    Serial.println(F("!! reinit NACKed"));
    return;
  }
  delay(D_REINIT);

  Serial.println();
  Serial.println(F("[4/4] POWER-CYCLE THE BOARD NOW."));
  Serial.println(F("Pull the USB/supply, wait a couple of seconds, plug it back in."));
  Serial.println(F("Then reopen this monitor and press [1] to verify the defaults"));
  Serial.println(F("came back: ASC = 1, T-offset ~4.00 C, altitude = 0."));
  Serial.println(F("If they did NOT change, the reset did not take — check that"));
  Serial.println(F("every endTransmission above read 0 and no measurement was running."));
  waitAnyKey(F("(press any key to return to the menu)"));
}

// ============================================================= Phase 5: FRC

static void phaseFRC() {
  rule();
  Serial.println(F("PHASE 5 — forced recalibration against real outdoor air"));
  rule();
  Serial.println(F("ENVIRONMENT: genuine outdoor air. A balcony or a wide-open"));
  Serial.println(F("window with air movement works. A 'well-ventilated room' does"));
  Serial.println(F("NOT. Put the sensor down and stand well away — a person at"));
  Serial.println(F("arm's length is a significant CO2 source."));
  Serial.print(F("TARGET: "));
  Serial.print(FRC_TARGET_PPM);
  Serial.println(F(" ppm."));
  Serial.println();
  waitAnyKey(F("Place the sensor in outdoor air, move away, then press any key."));

  Serial.println();
  Serial.println(F("[1/3] start periodic measurement"));
  if (sendCmd(CMD_START_PERIODIC, F("start_periodic_measurement")) != 0) {
    Serial.println(F("!! could not start measurement — aborting."));
    return;
  }
  g_measuring = true;

  Serial.println();
  Serial.print(F("[2/3] stabilising for "));
  Serial.print(FRC_WARMUP_MS / 60000UL);
  Serial.println(F(" minutes. Readings of 0 here are EXPECTED and not a"));
  Serial.println(F("       reason to abort — the raw signal still needs to settle."));
  Serial.println(F("       Press any key to abort without recalibrating."));
  Serial.println();

  const uint32_t started = millis();
  flushInput();
  while (millis() - started < FRC_WARMUP_MS) {
    if (Serial.available()) {
      flushInput();
      Serial.println(F("!! ABORTED by keypress — FRC was NOT performed."));
      Serial.println(F("!! Sensor left in periodic measurement."));
      return;
    }

    uint32_t remaining = (FRC_WARMUP_MS - (millis() - started)) / 1000UL;
    uint16_t w;
    if (sendCmd(CMD_DATA_READY, F("get_data_ready_status")) == 0) {
      delay(D_SHORT);
      if (readWords(&w, 1) && dataReady(w)) {
        if (sendCmd(CMD_READ_MEASUREMENT, F("read_measurement")) == 0) {
          delay(D_SHORT);
          uint16_t m[3];
          if (readWords(m, 3)) {
            Serial.print(F("   >> CO2 = "));
            Serial.print(m[0]);
            Serial.print(F(" ppm    T = "));
            Serial.print(decodeTemp(m[1]), 1);
            Serial.print(F(" C    RH = "));
            Serial.print(decodeHumidity(m[2]), 0);
            Serial.print(F(" %    settling, "));
            Serial.print(remaining);
            Serial.println(F("s to go"));
          }
        }
      }
    }
    delay(5000);
  }

  Serial.println();
  Serial.println(F("[3/3] stop measurement, then recalibrate"));
  if (sendCmd(CMD_STOP_PERIODIC, F("stop_periodic_measurement")) != 0) {
    Serial.println(F("!! stop failed — NOT issuing FRC, it would be discarded."));
    return;
  }
  delay(D_STOP_PERIODIC);
  g_measuring = false;

  if (sendCmdArg(CMD_FRC, FRC_TARGET_PPM, F("perform_forced_recalibration")) != 0) {
    Serial.println(F("!! FRC command NACKed — it did not happen."));
    return;
  }
  delay(D_FRC);

  uint16_t w;
  if (!readWords(&w, 1)) {
    Serial.println(F("!! could not read the FRC result — status unknown."));
    return;
  }

  Serial.println();
  rule();
  if (w == 0xFFFF) {
    Serial.println(F("!! FRC FAILED (returned 0xFFFF)."));
    Serial.println(F("!! The sensor was in the wrong state. Confirm periodic"));
    Serial.println(F("!! measurement really stopped and the 500 ms delay elapsed."));
    Serial.println(F("!! NOT a success — do not treat the sensor as recovered."));
    rule();
    return;
  }

  const int32_t correction = (int32_t)w - 0x8000;
  Serial.print(F(">> FRC OK. Baseline correction = "));
  Serial.print(correction);
  Serial.println(F(" ppm"));
  if (correction < -500) {
    Serial.println(F(">> A large negative correction directly CONFIRMS the drifted-"));
    Serial.println(F(">> baseline hypothesis, and tells you how far off it was."));
  }
  rule();

  Serial.println();
  Serial.println(F("Streaming for 2 minutes so you can confirm plausible ambient"));
  Serial.println(F("values (indoors: ~400-1200 ppm depending on ventilation)."));
  if (sendCmd(CMD_START_PERIODIC, F("start_periodic_measurement")) != 0) return;
  g_measuring = true;

  // The first sample after periodic measurement restarts is routinely invalid.
  Serial.println(F("(ignore the first reading or two)"));
  const uint32_t streamStart = millis();
  while (millis() - streamStart < 120000UL) {
    uint16_t st;
    if (sendCmd(CMD_DATA_READY, F("get_data_ready_status")) != 0) break;
    delay(D_SHORT);
    if (readWords(&st, 1) && dataReady(st)) {
      if (sendCmd(CMD_READ_MEASUREMENT, F("read_measurement")) != 0) break;
      delay(D_SHORT);
      uint16_t m[3];
      if (readWords(m, 3)) {
        Serial.print(F("   >> CO2 = "));
        Serial.print(m[0]);
        Serial.print(F(" ppm    T = "));
        Serial.print(decodeTemp(m[1]), 1);
        Serial.print(F(" C    RH = "));
        Serial.print(decodeHumidity(m[2]), 0);
        Serial.println(F(" %"));
      }
    }
    delay(5000);
  }
  Serial.println(F("Done. If values look right, consider [6] to disable ASC,"));
  Serial.println(F("then flash hub_node.ino back."));
}

// ================================================ Phase 6: lock in settings

// persist_settings writes an EEPROM rated for only ~2000 cycles. Call it only
// when a setting actually changed — NEVER in a loop or a per-boot routine.
static void persistAndVerifyASC(bool enable) {
  rule();
  Serial.print(F("PHASE 6 — "));
  Serial.print(enable ? F("ENABLE") : F("DISABLE"));
  Serial.println(F(" ASC + persist"));
  rule();
  if (!requireIdle(F("set_automatic_self_calibration_enabled"))) return;

  if (!enable) {
    Serial.println(F("Disabling ASC is right if the sensor lives in an enclosure or"));
    Serial.println(F("any room that does not reliably see outdoor air every few days."));
    Serial.println(F("Otherwise ASC silently re-corrupts the baseline within about a"));
    Serial.println(F("week and you are back here. Plan a manual FRC every few months."));
    Serial.println();
  }

  if (sendCmdArg(CMD_SET_ASC, enable ? 1 : 0, F("set_automatic_self_calibration_enabled")) != 0) return;
  delay(D_SHORT);

  Serial.println();
  if (sendCmd(CMD_PERSIST_SETTINGS, F("persist_settings")) != 0) {
    Serial.println(F("!! persist NACKed — the change is RAM-only and dies at power-off."));
    return;
  }
  delay(D_PERSIST);

  Serial.println();
  Serial.println(F("Reading back to confirm:"));
  if (sendCmd(CMD_GET_ASC, F("get_automatic_self_calibration_enabled")) != 0) return;
  delay(D_SHORT);
  uint16_t w;
  if (!readWords(&w, 1)) return;

  Serial.print(F("   ASC now = "));
  Serial.println(w);
  if ((w != 0) == enable) {
    Serial.println(F(">> confirmed, and written to EEPROM (survives power-off)."));
  } else {
    Serial.println(F("!! READ-BACK MISMATCH — the setting did not take."));
  }
}

static void phaseSetAltitude() {
  rule();
  Serial.println(F("PHASE 6b — sensor altitude + persist"));
  rule();
  if (!requireIdle(F("set_sensor_altitude"))) return;

  Serial.println(F("Altitude above sea level, for pressure compensation."));
  long metres = readNumber(F("altitude in metres"), 0, 3000);
  if (metres == READ_CANCELLED) return;

  if (sendCmdArg(CMD_SET_ALTITUDE, (uint16_t)metres, F("set_sensor_altitude")) != 0) return;
  delay(D_SHORT);

  Serial.println();
  if (sendCmd(CMD_PERSIST_SETTINGS, F("persist_settings")) != 0) {
    Serial.println(F("!! persist NACKed — RAM-only, dies at power-off."));
    return;
  }
  delay(D_PERSIST);

  Serial.println();
  Serial.println(F("Reading back to confirm:"));
  if (sendCmd(CMD_GET_ALTITUDE, F("get_sensor_altitude")) != 0) return;
  delay(D_SHORT);
  uint16_t w;
  if (readWords(&w, 1)) {
    Serial.print(F("   altitude now = "));
    Serial.print(w);
    Serial.println(F(" m"));
    if (w == (uint16_t)metres) Serial.println(F(">> confirmed and persisted."));
    else Serial.println(F("!! READ-BACK MISMATCH — the setting did not take."));
  }
}

// ================================================================ menu

static void printMenu() {
  Serial.println();
  rule();
  Serial.println(F("  SCD40 RECOVERY                      state: measurement is"));
  Serial.print(F("                                      "));
  Serial.println(g_measuring ? F("RUNNING") : F("STOPPED"));
  rule();
  Serial.println(F("  [0] Bus scan + serial number      (identity / wiring check)"));
  Serial.println(F("  [1] Read full sensor state        (ASC, T-offset, altitude)"));
  Serial.println(F("  [2] Self-test                     (10 s - hardware verdict)"));
  Serial.println(F("  [3] Continuous measurement        (any key stops)"));
  Serial.println(F("  [4] Clean factory reset           (full correct sequence)"));
  Serial.println(F("  [5] Forced recalibration (FRC)    (guided, 5 min warm-up)"));
  Serial.println(F("  [6] Disable ASC + persist"));
  Serial.println(F("  [7] Enable ASC + persist"));
  Serial.println(F("  [8] Set sensor altitude + persist"));
  rule();
  Serial.println(F("  Suggested order: 0 -> 2 -> 1 -> 4 -> (power cycle) -> 5 -> 6"));
  Serial.print(F("  > "));
}

void setup() {
  Serial.begin(115200);
  // Wait for the USB monitor, but give up after 5 s so this still runs headless.
  const uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 5000) { /* wait */ }

  Wire.begin();
  Wire.setClock(I2C_CLOCK_HZ);
  delay(D_POWER_UP);   // datasheet: 1000 ms after VDD stable before first command

  Serial.println();
  Serial.println(F("scd40_recovery — raw-I2C SCD40 diagnostic tool"));
  Serial.print(F("I2C clock: "));
  Serial.print(I2C_CLOCK_HZ / 1000);
  Serial.println(F(" kHz (the SCD4x is NOT 400 kHz rated)"));

  // The sensor may still be in periodic measurement from a previous sketch,
  // and we cannot ask it. Stop unconditionally so the state is known — this is
  // the safe assumption, since every restricted command depends on it.
  Serial.println();
  Serial.println(F("Forcing a known state (previous firmware may have left"));
  Serial.println(F("periodic measurement running):"));
  sendCmd(CMD_STOP_PERIODIC, F("stop_periodic_measurement"));
  delay(D_STOP_PERIODIC);
  g_measuring = false;

  printMenu();
}

void loop() {
  if (!Serial.available()) return;
  int c = Serial.read();
  if (c == '\r' || c == '\n') return;

  Serial.println((char)c);
  switch (c) {
    case '0': phaseBusScan();        break;
    case '1': phaseReadConfig();     break;
    case '2': phaseSelfTest();       break;
    case '3': phaseContinuous();     break;
    case '4': phaseFactoryReset();   break;
    case '5': phaseFRC();            break;
    case '6': persistAndVerifyASC(false); break;
    case '7': persistAndVerifyASC(true);  break;
    case '8': phaseSetAltitude();    break;
    default:
      Serial.print(F("unknown option '"));
      Serial.print((char)c);
      Serial.println(F("'"));
      break;
  }
  printMenu();
}
