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
// I2C on the Due: SDA = pin 20, SCL = pin 21 (labeled on the board).

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

void setup() {
  Serial.begin(115200);
  rxBuffer.reserve(32);

  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_RELAY1, OUTPUT);
  digitalWrite(PIN_RELAY1, LOW);
  pinMode(PIN_DIM1, OUTPUT);
  analogWrite(PIN_DIM1, 0);

  Wire.begin();

  // Try both common BME280 addresses (0x76 on most clone breakouts, 0x77 Adafruit)
  bmeOk = bme.begin(0x76) || bme.begin(0x77);
  bhOk = lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE);
  scdOk = co2Sensor.begin();  // starts periodic measurement by default

  // '#' lines are logs; the host parser skips them.
  Serial.println(F("# hub_node boot"));
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
    Serial.print(F("CO2:"));
    Serial.println(co2Sensor.getCO2());
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
