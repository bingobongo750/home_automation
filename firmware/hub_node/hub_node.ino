/*
 * hub_node.ino — Smart Home Hub, wired sensor node (Arduino Due)
 *
 * Reads BME280 (temp/hum), BH1750 (lux), SCD40 (CO2) over I2C plus an
 * HC-SR501 PIR on a digital pin, and publishes readings over USB serial
 * (the Due's PROGRAMMING port — the one nearer the DC jack).
 *
 * VOLTAGE: the Due is 3.3V logic and its pins are NOT 5V tolerant. Power
 * every I2C breakout from the 3.3V pin so the bus is pulled up to 3.3V.
 * The HC-SR501 is the one exception: feed it from the 5V pin (its regulator
 * needs it) — its output signal is natively 3.3V and safe on PIN_PIR.
 *
 * SERIAL PROTOCOL (keep in sync with /docs/serial-protocol.md and the
 * host-side parser in /server/app/serial_reader.py):
 *
 *   Arduino -> Host, one reading per line, sent every REPORT_INTERVAL_MS:
 *     TEMP:21.4        degrees C, 1 decimal
 *     HUM:47.2         % relative humidity, 1 decimal
 *     LUX:312          lux, integer
 *     CO2:612          ppm, integer
 *     MOTION:1         0/1, sent on every report AND immediately on change
 *
 *   Host -> Arduino, commands, parsed with readStringUntil('\n'):
 *     RELAY1:ON | RELAY1:OFF     relay module (stub — not wired yet)
 *     DIM1:<0-255>               MOSFET PWM dim level (stub)
 *     MODE:<name>                NeoPixel mode select (stub)
 *     COLOR:r,g,b                NeoPixel direct color (stub)
 *
 *   Lines starting with '#' are human-readable log/debug output; the host
 *   ignores them. All lines end with '\n'.
 *
 * Timing is non-blocking (millis() scheduling, no long delay()) so incoming
 * serial commands stay responsive between sensor reports.
 */

#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include <BH1750.h>
#include <SparkFun_SCD4x_Arduino_Library.h>

// ---- Pin assignments (see firmware/README.md for wiring) ----
const uint8_t PIN_PIR      = 2;   // HC-SR501 output
const uint8_t PIN_RELAY1   = 7;   // future: opto-isolated relay module IN1
const uint8_t PIN_DIM1     = 9;   // future: IRLZ44N gate (PWM-capable pin)
const uint8_t PIN_NEOPIXEL = 6;   // future: WS2812B data line
// I2C on the Due: SDA = pin 20, SCL = pin 21 (labeled on the board). Named here
// because recoverI2C() drives them directly, before Wire.begin() claims them.
const uint8_t PIN_SDA      = 20;
const uint8_t PIN_SCL      = 21;

// ---- Timing ----
const unsigned long REPORT_INTERVAL_MS = 5000;  // sensor report cadence
unsigned long lastReportMs = 0;

// ---- Sensors ----
Adafruit_BME280 bme;
BH1750 lightMeter;
SCD4x co2Sensor;

bool bmeOk = false;
bool bhOk = false;
bool scdOk = false;

// ---- State ----
int lastMotion = 0;
String rxBuffer;  // reserve()d in setup to limit heap fragmentation

// A slave that was reset mid-transfer (which happens every time the Due is
// reset or reflashed, since the sensors keep their 3.3V rail) can hold SDA low
// indefinitely, wedging the whole bus — every device then looks absent. The
// standard fix is to clock SCL until the slave finishes the byte it thinks it
// is sending, then issue a STOP. Cheap, safe, and it turns a "drive over and
// unplug it" failure into a self-healing one on a 24/7 box.
//
// Runs before Wire.begin() takes the pins. Logs the idle line levels, which
// distinguish the three failure modes: both HIGH = healthy idle bus, SDA LOW =
// wedged slave, both LOW = no pull-ups (sensor power gone, or a short).
void recoverI2C() {
  pinMode(PIN_SDA, INPUT_PULLUP);
  pinMode(PIN_SCL, INPUT_PULLUP);
  delayMicroseconds(10);

  Serial.print(F("# I2C idle: SDA="));
  Serial.print(digitalRead(PIN_SDA) == HIGH ? F("HIGH") : F("LOW"));
  Serial.print(F(" SCL="));
  Serial.println(digitalRead(PIN_SCL) == HIGH ? F("HIGH") : F("LOW"));

  if (digitalRead(PIN_SDA) == HIGH) {
    return;  // bus is idle, nothing to recover
  }

  Serial.println(F("# I2C: SDA held low — clocking the bus free"));
  pinMode(PIN_SCL, OUTPUT);
  for (uint8_t i = 0; i < 9 && digitalRead(PIN_SDA) == LOW; i++) {
    digitalWrite(PIN_SCL, LOW);
    delayMicroseconds(5);
    digitalWrite(PIN_SCL, HIGH);
    delayMicroseconds(5);
  }

  // STOP condition: SDA rises while SCL is high.
  pinMode(PIN_SDA, OUTPUT);
  digitalWrite(PIN_SDA, LOW);
  delayMicroseconds(5);
  digitalWrite(PIN_SCL, HIGH);
  delayMicroseconds(5);
  digitalWrite(PIN_SDA, HIGH);
  delayMicroseconds(5);

  pinMode(PIN_SDA, INPUT_PULLUP);
  pinMode(PIN_SCL, INPUT_PULLUP);
  Serial.print(F("# I2C: after recovery SDA="));
  Serial.println(digitalRead(PIN_SDA) == HIGH ? F("HIGH (freed)") : F("LOW (still stuck)"));
}

// Bring-up diagnostic: list every address that ACKs on the bus, so a missing
// sensor can be told apart from a dead bus without swapping in a scanner sketch.
// Prints a '#' log line, which the host parser ignores.
void scanI2C() {
  Serial.print(F("# I2C scan:"));
  uint8_t found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.print(F(" 0x"));
      Serial.print(addr, HEX);
      found++;
    }
  }
  if (found == 0) {
    Serial.print(F(" nothing found"));
  }
  Serial.println();
}

// Take one BME280 reading in FORCED mode, then leave the sensor asleep.
//
// WHY NOT THE DEFAULTS: Adafruit's begin() leaves MODE_NORMAL with x16
// oversampling on all three channels and a 0.5 ms standby — converting
// essentially continuously at maximum rate. Those defaults are chosen for
// out-of-the-box responsiveness, not accuracy. Forced mode at x1 on a 5 s
// cadence is Bosch's own "weather monitoring" recommendation and leaves the
// part asleep between reads, at roughly 0.3 % duty cycle.
//
// MEASURED, so nobody repeats the experiment: this change was made expecting
// it to cut a self-heating offset, and it did NOT. Over 21 minutes of settled
// operation the reading moved from ~28.03 C to ~27.95 C — about 0.08 C, an
// order of magnitude inside the sensor's own +/-0.5 C spec, i.e. nothing.
// Self-heating is not why this board reads warm. The SCD40 on the same bus, a
// different chip from a different manufacturer, independently read 30.2-31.3 C
// at the same time; two unrelated parts agreeing was never going to be
// explained by each heating itself. The reading is real and the cause is
// placement — see MANUAL.md section 7.11. Keep forced mode anyway: it is the
// correct way to run the part and costs nothing, just do not expect degrees
// from it.
//
// Pressure is SAMPLING_NONE because nothing in the hub reads it; skipping it
// shortens each conversion. Temperature and humidity are unaffected (humidity
// compensation needs temperature, not pressure).
//
// WHY NOT takeForcedMeasurement(): it polls the status register in an
// UNBOUNDED `while` loop. A failed I2C read there returns 0xFF, whose
// "measuring" bit is set, so a wedged bus hangs the sketch forever — producing
// exactly nothing on serial, which is indistinguishable from a dead board. On a
// box that runs unattended, a bounded wait that might occasionally return a
// stale value is far better than a silent lockup. Re-issuing setSampling()
// writes ctrl_meas with MODE_FORCED, and that write is what starts a single
// conversion.
static void bmeMeasure() {
  bme.setSampling(Adafruit_BME280::MODE_FORCED,
                  Adafruit_BME280::SAMPLING_X1,    // temperature
                  Adafruit_BME280::SAMPLING_NONE,  // pressure — unused
                  Adafruit_BME280::SAMPLING_X1,    // humidity
                  Adafruit_BME280::FILTER_OFF);
  // Datasheet t_measure,max for x1 T + x1 H is ~6.4 ms. 15 ms is comfortable
  // and still ~0.3 % duty cycle at a 5 s report interval.
  delay(15);
}

void setup() {
  Serial.begin(115200);
  rxBuffer.reserve(32);

  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_RELAY1, OUTPUT);
  digitalWrite(PIN_RELAY1, LOW);
  pinMode(PIN_DIM1, OUTPUT);
  analogWrite(PIN_DIM1, 0);

  // '#' lines are logs; the host parser skips them.
  Serial.println(F("# hub_node boot"));
  recoverI2C();  // must run before Wire.begin() claims SDA/SCL

  Wire.begin();
  scanI2C();  // what is actually on the bus, before any driver touches it

  // Try both common BME280 addresses (0x76 on most clone breakouts, 0x77 Adafruit)
  bmeOk = bme.begin(0x76) || bme.begin(0x77);
  // Drop straight out of the library's free-running default (see bmeMeasure)
  // so the sensor is not self-heating while the other drivers start up.
  if (bmeOk) bmeMeasure();
  bhOk = lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE);
  scdOk = co2Sensor.begin();  // starts periodic measurement by default

  Serial.print(F("# BME280: "));  Serial.println(bmeOk ? F("ok") : F("NOT FOUND"));
  Serial.print(F("# BH1750: "));  Serial.println(bhOk ? F("ok") : F("NOT FOUND"));
  Serial.print(F("# SCD40:  "));  Serial.println(scdOk ? F("ok") : F("NOT FOUND"));
}

void loop() {
  pollSerialCommands();

  // Motion changes are reported immediately, not just on the report tick,
  // so the dashboard's presence view feels live.
  int motion = digitalRead(PIN_PIR) == HIGH ? 1 : 0;
  if (motion != lastMotion) {
    lastMotion = motion;
    Serial.print(F("MOTION:"));
    Serial.println(motion);
  }

  unsigned long now = millis();
  if (now - lastReportMs >= REPORT_INTERVAL_MS) {
    lastReportMs = now;
    reportSensors();
  }
}

// ---------------------------------------------------------------- reporting

void reportSensors() {
  if (bmeOk) {
    bmeMeasure();  // forced mode sleeps between reads — wake it for this one
    Serial.print(F("TEMP:"));
    Serial.println(bme.readTemperature(), 1);
    Serial.print(F("HUM:"));
    Serial.println(bme.readHumidity(), 1);
  }

  if (bhOk) {
    float lux = lightMeter.readLightLevel();
    if (lux >= 0) {  // negative return = read error
      Serial.print(F("LUX:"));
      Serial.println((long)lux);
    }
  }

  // SCD40 self-paces (~5s measurement interval); readMeasurement() returns
  // true only when a fresh sample was fetched, so stale ticks are skipped.
  if (scdOk && co2Sensor.readMeasurement()) {
    uint16_t co2 = co2Sensor.getCO2();
    Serial.print(F("CO2:"));
    Serial.println(co2);

    // Bring-up diagnostic, remove once CO2 reads sanely. The same 9-byte frame
    // carries T and RH: an all-zero frame reads t=-45.00 rh=0.0 (the sensor has
    // produced no measurement), whereas plausible T/RH with co2=0 means only the
    // CO2 channel is invalid. The two have completely different causes.
    if (co2 == 0) {
      Serial.print(F("# SCD40 zero frame: t="));
      Serial.print(co2Sensor.getTemperature(), 2);
      Serial.print(F(" rh="));
      Serial.println(co2Sensor.getHumidity(), 1);
    }
  }

  Serial.print(F("MOTION:"));
  Serial.println(lastMotion);
}

// ----------------------------------------------------------------- commands

void pollSerialCommands() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      rxBuffer.trim();  // strip \r from hosts that send \r\n
      if (rxBuffer.length() > 0) {
        handleCommand(rxBuffer);
      }
      rxBuffer = "";
    } else if (rxBuffer.length() < 31) {
      rxBuffer += c;
    }
  }
}

void handleCommand(const String &cmd) {
  int sep = cmd.indexOf(':');
  if (sep < 0) {
    Serial.print(F("# ERR bad command: "));
    Serial.println(cmd);
    return;
  }
  String key = cmd.substring(0, sep);
  String value = cmd.substring(sep + 1);

  if (key == "RELAY1") {
    // Stub: relay module not wired yet. Pin is driven so behavior is
    // already correct the day the relay lands on PIN_RELAY1.
    digitalWrite(PIN_RELAY1, value == "ON" ? HIGH : LOW);
    Serial.print(F("# RELAY1 set "));
    Serial.println(value);
  } else if (key == "DIM1") {
    int level = constrain(value.toInt(), 0, 255);
    analogWrite(PIN_DIM1, level);  // stub: MOSFET not wired yet
    Serial.print(F("# DIM1 set "));
    Serial.println(level);
  } else if (key == "MODE") {
    // Stub: NeoPixel strip not wired yet. Mode logic (aqi traffic light,
    // motion accent, sunrise) will live here, entirely on the Arduino.
    Serial.print(F("# MODE set "));
    Serial.println(value);
  } else if (key == "COLOR") {
    Serial.print(F("# COLOR set "));
    Serial.println(value);  // "r,g,b" — parsed for real once strip exists
  } else {
    Serial.print(F("# ERR unknown command: "));
    Serial.println(cmd);
  }
}
