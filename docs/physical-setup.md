# Physical setup — to do when the hardware is at hand

The software is complete and runs against `MOCK_HARDWARE=1` until these are
done. Nothing below blocks software work.

## myStrom plug provisioning

1. Power the plug; provision it onto the home WiFi with the myStrom app (this
   is the only time the app/cloud is touched — runtime control is local-only).
2. On the router, give the plug a **static DHCP reservation**.
3. Put that IP in `.env` as `MYSTROM_PLUG_IP`, and update the seeded device
   row if it differs (`UPDATE devices SET ip = '...' WHERE type = 'wifi_plug';`
   — or just delete the DB and let it reseed from `.env`).
4. Sanity check from the Mac: `curl http://<plug-ip>/report` should return
   JSON with `power` and `relay` fields.

## Breadboard wiring (Arduino Uno)

Pinout lives in `/firmware/README.md`. Summary: BME280 + BH1750 + SCD30 share
I2C on A4 (SDA) / A5 (SCL); PIR output on D2; future relay D7, MOSFET gate D9,
NeoPixel data D6. Use Adafruit-style breakouts only (onboard 3.3V
regulation/level shifting) — the Uno is 5V logic.

- HC-SR501: let it warm up ~60 s after power-on before trusting `MOTION`
  readings; tune its sensitivity/hold-time trimpots in place.
- SCD30: needs a couple of minutes to settle; consider its calibration
  (fresh outdoor air ≈ 420 ppm) after installation.

## Host bring-up

1. Plug the Arduino into the Mac; find the port: `ls /dev/tty.usb*` and set
   `SERIAL_PORT` in `.env`.
2. Point `DB_PATH` at the 1TB SSD (e.g. `/Volumes/SSD/home_automation/home.db`).
3. Remove `MOCK_HARDWARE=1`, restart the server, and watch the log: it should
   print `Serial connected`, sensor rows should start landing, and the plug
   poller should stop logging `PLUG UNREACHABLE`.
4. Reach the dashboard remotely via Tailscale at `http://<mac-tailnet-name>:8000`.
5. For 24/7 operation, run the server under `launchd` (a LaunchAgent with
   `KeepAlive`) so it restarts on crash/reboot — not yet set up.
