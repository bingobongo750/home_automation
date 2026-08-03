/*
 * scd40_calibrate.ino — one-shot SCD40 forced recalibration (FRC).
 *
 * Throwaway bring-up tool, NOT part of the hub. Flash it, fix the sensor's
 * baseline, then flash hub_node.ino back.
 *
 * WHY: the SCD4x reports (measured CO2 - stored baseline offset) as an unsigned
 * value, so once the baseline is wrong-high, ordinary room air lands below it
 * and reads 0 ppm. Automatic self-calibration maintains that baseline on the
 * assumption the sensor regularly sees fresh outdoor air (~420 ppm) and treats
 * the lowest concentration in its recent history as that value — so a sensor
 * that never gets aired out, or arrived in a bad factory state, ends up pinned
 * at 0. FRC overwrites the baseline in one shot: you put it in air of known
 * concentration and tell it what it is looking at.
 *
 * PROCEDURE (Sensirion's, and the order matters):
 *   1. Run periodic measurement for >= 3 minutes in the reference air.
 *   2. Stop periodic measurement, wait 500 ms.
 *   3. Issue FRC with the reference concentration.
 *   4. Restart periodic measurement.
 * Doing step 3 early just stores a new wrong baseline, so it is guarded below.
 *
 * USAGE: with AUTO_FRC (default), power the board outside from a battery and
 * walk away — the FRC fires itself once the countdown expires. Or watch it on
 * the serial monitor at 115200 and send 'c' by hand. Either way, 'f' performs a
 * factory reset if FRC refuses.
 *
 * Wiring is unchanged from hub_node.ino — SCD40 on the shared I2C bus (0x62),
 * powered from 3.3V.
 */

#include <Wire.h>
#include <SparkFun_SCD4x_Arduino_Library.h>

// Outdoor air. Global average is ~420 ppm and rising a couple of ppm a year;
// this only needs to be right to within a few tens of ppm.
const uint16_t REFERENCE_PPM = 420;

// Sensirion's minimum equilibration time in the reference air before FRC.
const unsigned long SETTLE_MS = 180000UL;

// Keep this matching hub_node.ino so the sensor is calibrated in the same
// configuration it will run in. Set false if the sensor lives somewhere that
// never gets aired out — ASC is what corrupted the baseline in that case, and
// a periodic manual FRC is the honest alternative.
const bool ENABLE_ASC = true;

// Fire the FRC automatically once the settle time expires, so the board can be
// left outside unattended (which also keeps your breath away from it).
//
// OFF by default, and it must stay that way for any indoor run. The obvious
// guard — only arm when every sample reads 0, i.e. the baseline is still broken
// — does not help here, because a sensor that needs calibrating reads 0
// EVERYWHERE, indoors included. So an unattended indoor boot would happily
// declare room air to be REFERENCE_PPM and make things worse.
//
// Set true only for the trip outside, and set it back afterwards. Manual 'c'
// (guarded by the settle timer) is the safe default for anything at a desk.
const bool AUTO_FRC = false;

SCD4x scd;
unsigned long settleStartMs = 0;
bool sawNonZero = false;   // any non-zero sample this power cycle
bool frcDone = false;      // one automatic attempt per power cycle

void setup() {
  Serial.begin(115200);
  while (!Serial);
  Wire.begin();

  Serial.println(F("# scd40_calibrate"));

  // measBegin = false: the identity registers only answer in idle mode, so read
  // them before periodic measurement starts.
  if (!scd.begin(Wire, false, ENABLE_ASC)) {
    Serial.println(F("SCD4x NOT FOUND — expected at 0x62. Check wiring and try again."));
    while (1);
  }

  char serialNumber[13];
  if (scd.getSerialNumber(serialNumber)) {
    Serial.print(F("serial number : 0x"));
    Serial.println(serialNumber);
  } else {
    Serial.println(F("serial number : READ FAILED"));
  }

  scd4x_sensor_type_e type;
  if (scd.getFeatureSetVersion(&type)) {
    Serial.print(F("sensor type   : "));
    if (type == SCD4x_SENSOR_SCD40)      Serial.println(F("SCD40"));
    else if (type == SCD4x_SENSOR_SCD41) Serial.println(F("SCD41"));
    else                                 Serial.println(F("INVALID"));
  }

  Serial.print(F("ASC           : "));
  Serial.println(ENABLE_ASC ? F("enabled") : F("disabled"));
  Serial.print(F("FRC reference : "));
  Serial.print(REFERENCE_PPM);
  Serial.println(F(" ppm"));
  Serial.println();
  Serial.println(F("Take the sensor outside now. Readings appear every 5 s."));
  Serial.println(F("After the countdown reaches 0, send 'c' to recalibrate."));
  Serial.println(F("Send 'f' for a factory reset (only if FRC fails)."));
  Serial.println();

  scd.startPeriodicMeasurement();
  settleStartMs = millis();
}

void loop() {
  if (scd.readMeasurement()) {
    unsigned long elapsed = millis() - settleStartMs;
    long remaining = ((long)SETTLE_MS - (long)elapsed) / 1000L;

    uint16_t co2 = scd.getCO2();
    if (co2 != 0) {
      sawNonZero = true;
    }

    Serial.print(F("co2="));
    Serial.print(co2);
    Serial.print(F(" ppm  t="));
    Serial.print(scd.getTemperature(), 1);
    Serial.print(F("C  rh="));
    Serial.print(scd.getHumidity(), 0);
    Serial.print(F("%   "));
    if (remaining > 0) {
      Serial.print(F("settling, "));
      Serial.print(remaining);
      Serial.println(F("s to go"));
    } else {
      Serial.println(F("READY — send 'c' to recalibrate"));
    }

    if (AUTO_FRC && !frcDone && remaining <= 0) {
      frcDone = true;
      if (sawNonZero) {
        Serial.println(F(">> auto-FRC skipped: sensor is already reporting non-zero, so its"));
        Serial.println(F(">> baseline is not the broken-at-zero case. Send 'c' to force it."));
      } else {
        Serial.println(F(">> auto-FRC: settle time elapsed, every sample read 0 ppm"));
        doForcedRecalibration();
      }
    }
  }

  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'c' || c == 'C') {
      doForcedRecalibration();
    } else if (c == 'f' || c == 'F') {
      doFactoryReset();
    }
    // anything else (newlines from the monitor) is ignored
  }
}

void doForcedRecalibration() {
  unsigned long elapsed = millis() - settleStartMs;
  if (elapsed < SETTLE_MS) {
    Serial.print(F("!! Too early — "));
    Serial.print((SETTLE_MS - elapsed) / 1000UL);
    Serial.println(F("s of settling left. Recalibrating now would store a new wrong baseline."));
    return;
  }

  Serial.println(F(">> stopping periodic measurement"));
  if (!scd.stopPeriodicMeasurement()) {  // waits 500 ms, as the datasheet requires
    Serial.println(F("!! stop failed — sensor did not ACK. Aborting."));
    return;
  }

  float correction = 0.0;
  if (scd.performForcedRecalibration(REFERENCE_PPM, &correction)) {
    Serial.print(F(">> FRC OK. Baseline moved by "));
    Serial.print(correction, 1);
    Serial.println(F(" ppm."));

    // Settings live in RAM until this is issued, so without it the calibration
    // is lost the moment the board loses power — e.g. carrying it back indoors.
    // Must run before periodic measurement restarts. The EEPROM is only good for
    // ~2000 writes, hence once per calibration, not once per boot.
    if (scd.persistSettings()) {
      Serial.println(F(">> settings written to EEPROM — survives a power cycle"));
    } else {
      Serial.println(F("!! persistSettings FAILED — calibration will be lost on power-off"));
    }

    // The first sample after periodic measurement restarts is routinely invalid,
    // so the very next line will often read 0 whether or not the FRC worked.
    Serial.println(F(">> IGNORE the next reading or two, then expect values near the reference."));
    Serial.println(F(">> Flash hub_node.ino back when happy."));
  } else {
    Serial.println(F("!! FRC REFUSED (sensor returned 0xFFFF)."));
    Serial.println(F("!! Send 'f' to factory reset, wait out the countdown again, then retry 'c'."));
  }

  scd.startPeriodicMeasurement();
  settleStartMs = millis();
  sawNonZero = false;
}

void doFactoryReset() {
  Serial.println(F(">> factory reset — clears ASC state and all stored calibration"));
  scd.stopPeriodicMeasurement();
  if (scd.performFactoryReset()) {
    Serial.println(F(">> done"));
  } else {
    Serial.println(F("!! factory reset failed"));
  }
  scd.setAutomaticSelfCalibrationEnabled(ENABLE_ASC);
  scd.startPeriodicMeasurement();
  settleStartMs = millis();
  sawNonZero = false;
  frcDone = false;   // a reset re-arms the automatic attempt
}
