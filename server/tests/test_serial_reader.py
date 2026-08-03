"""Serial ingest tests — the plausibility band and the pause/resume control.
Runs against MOCK_HARDWARE=1, no threads, no physical devices. From server/:

    python3 -m unittest discover -s tests
"""

import os
import tempfile
import unittest

# config.py reads these at import time — set them before touching app.*
_TMP = tempfile.mkdtemp(prefix="hub-test-")
os.environ["MOCK_HARDWARE"] = "1"
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")

from flask import Flask  # noqa: E402

from app import db, serial_reader  # noqa: E402
from app.api import api  # noqa: E402


class HandleLineTestCase(unittest.TestCase):
    def setUp(self):
        db.init_db()
        with db.connect() as conn:
            conn.execute("DELETE FROM readings")
        serial_reader._rejects.clear()

    def stored(self, metric):
        with db.connect() as conn:
            return [
                r["value"] for r in conn.execute(
                    "SELECT value FROM readings WHERE metric = ? ORDER BY id", (metric,)
                )
            ]

    def test_plausible_readings_are_stored(self):
        for line in ("TEMP:21.4", "HUM:47.2", "LUX:312", "CO2:612", "MOTION:1"):
            serial_reader.handle_line(line)
        self.assertEqual(self.stored("temp"), [21.4])
        self.assertEqual(self.stored("co2"), [612.0])
        self.assertEqual(self.stored("motion"), [1.0])

    def test_zero_ppm_co2_is_stored_not_dropped(self):
        """The failing SCD40's sentinel MUST reach the DB — the dashboard is
        where this fault gets reviewed, so ingest never hides it."""
        serial_reader.handle_line("CO2:0")
        self.assertEqual(self.stored("co2"), [0.0])

    def test_implausible_values_are_logged_but_still_stored(self):
        with self.assertLogs("serial", level="WARNING"):
            serial_reader.handle_line("CO2:0")
        self.assertEqual(self.stored("co2"), [0.0])

    def test_a_good_reading_after_a_bad_one_still_lands(self):
        serial_reader.handle_line("CO2:0")
        serial_reader.handle_line("CO2:True")   # non-numeric: still dropped
        serial_reader.handle_line("CO2:588")
        self.assertEqual(self.stored("co2"), [0.0, 588.0])

    def test_out_of_band_values_are_kept_at_both_edges(self):
        for line in ("HUM:0", "HUM:100", "HUM:100.1", "HUM:-0.1"):
            serial_reader.handle_line(line)
        self.assertEqual(self.stored("hum"), [0.0, 100.0, 100.1, -0.1])

    def test_a_dead_co2_channel_does_not_disturb_other_metrics(self):
        for line in ("TEMP:21.4", "CO2:0", "HUM:47.2", "CO2:0", "LUX:312"):
            serial_reader.handle_line(line)
        self.assertEqual(self.stored("temp"), [21.4])
        self.assertEqual(self.stored("hum"), [47.2])
        self.assertEqual(self.stored("lux"), [312.0])
        self.assertEqual(self.stored("co2"), [0.0, 0.0])

    def test_firmware_log_lines_are_never_stored(self):
        serial_reader.handle_line("# SCD40 zero frame: t=-45.00 rh=0.0")
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"], 0)

    def test_implausible_logging_is_throttled(self):
        with self.assertLogs("serial", level="WARNING") as caught:
            for _ in range(50):
                serial_reader.handle_line("CO2:0")
            serial_reader.handle_line("CO2:0")  # keeps at least one record
        self.assertEqual(len(caught.records), 1, "a dead sensor must not flood the log")
        self.assertEqual(len(self.stored("co2")), 51, "throttling logs, not storage")


class SerialPauseTestCase(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(api)
        self.client = app.test_client()
        self.addCleanup(serial_reader.resume)

    def test_pause_then_resume_round_trip(self):
        r = self.client.post("/api/arduino/serial", json={"paused": True, "minutes": 5})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["paused"])
        self.assertTrue(serial_reader.is_paused())
        self.assertTrue(self.client.get("/api/arduino/serial").get_json()["paused"])

        r = self.client.post("/api/arduino/serial", json={"paused": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(serial_reader.is_paused())

    def test_pause_expires_on_its_own(self):
        """An unattended box must never be left permanently mute."""
        serial_reader.pause(1)
        self.assertTrue(serial_reader.is_paused())
        with serial_reader._pause_lock:
            timer = serial_reader._resume_timer
        timer.join(5)
        self.assertFalse(serial_reader.is_paused())

    def test_pause_duration_is_capped(self):
        r = self.client.post("/api/arduino/serial", json={"paused": True, "minutes": 10000})
        self.assertEqual(
            r.get_json()["resumes_in_minutes"], round(serial_reader.MAX_PAUSE_S / 60, 1)
        )

    def test_bad_bodies_are_rejected(self):
        self.assertEqual(self.client.post("/api/arduino/serial", json={}).status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/arduino/serial", json={"paused": True, "minutes": "soon"}
            ).status_code,
            400,
        )

    def test_resume_is_idempotent(self):
        serial_reader.resume()
        serial_reader.resume()
        self.assertFalse(serial_reader.is_paused())


if __name__ == "__main__":
    unittest.main()
