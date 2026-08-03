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

## Bulb zone provisioning (per lighting zone)

The software (backend, mock bulbs, dashboard cards) is complete and runs
against `MOCK_HARDWARE=1`. Per zone (e.g. "Cupboard", "Room LED"), when the
hardware is at hand:

1. Screw a **Shelly Multicolor Bulb E27 Gen3** into the zone's E27 fixture.
   The bulb is the entire device — no controller, no strip, no soldering.
   The fixture's wall switch has to stay ON permanently; cutting mains makes
   the bulb unreachable.
2. Provision the bulb onto the home WiFi via the Shelly Smart Control app,
   or app-free by joining its AP and opening `http://192.168.33.1` (only
   time any app/portal is touched — runtime control is local-only, same as
   the myStrom plug). Cloud can stay disabled.
3. On the router, give it a **static DHCP reservation**.
4. Put that IP in `.env` as `SHELLY_CUPBOARD_IP` / `SHELLY_ROOM_LED_IP` (or add
   a new `SHELLY_<ZONE>_IP` + a row in `BULB_SEEDS` in `app/db.py` for a
   third zone), and update the seeded device row if it differs
   (`UPDATE devices SET ip = '...' WHERE name = 'Cupboard';` — or delete the
   DB and let it reseed from `.env`).
5. Sanity check from the Mac:
   `curl "http://<zone-ip>/rpc/RGBCCT.GetStatus?id=0"` should return JSON
   with `output`, `brightness`, and `rgb` fields. Note it's `RGBCCT.*`, not
   the `Light.*` methods used by simpler Shelly dimmers.
6. If a zone is meant to auto-dim with ambient light, set its mode to
   `auto` from the dashboard's Lighting card (or
   `POST /api/devices/:id/mode {"mode": "auto"}`) once the BH1750 is
   installed and reporting `lux`.

## Breadboard wiring (Arduino Due)

Pinout lives in `/firmware/README.md`. Summary: BME280 + BH1750 + SCD40 share
I2C on SDA (20) / SCL (21); PIR output on D2; future relay D7, MOSFET gate D9,
NeoPixel data D6.

**The Due is 3.3V logic and its pins are NOT 5V tolerant.** Power the I2C
breakouts (Adafruit-style boards with onboard regulation only, never bare
chips) from the **3.3V pin** so the bus is pulled up to 3.3V — never from 5V.

- HC-SR501: the exception — feed it from the **5V pin** (its onboard regulator
  needs it); its output signal is natively 3.3V and safe on D2. Let it warm up
  ~60 s after power-on before trusting `MOTION` readings; tune its
  sensitivity/hold-time trimpots in place.
- SCD40: needs a minute or two to settle; automatic self-calibration assumes
  the room sees fresh air (≈ 420 ppm) regularly — force a recalibration
  outdoors if readings look off after installation.
- Use the Due's **programming port** (micro-USB nearer the DC jack) — that's
  the `Serial` the firmware and host read.

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
