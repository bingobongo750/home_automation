/* HUB-01 dashboard — vanilla JS, no build step.
   Two views behind the header VIEW switch: BOARD (the one-page widget
   board — live values poll every 5s; sparklines and an open detail dialog
   refresh every 30s; clicking a widget opens its detail in an overlay
   dialog so nothing on the board moves; the switch cards control the plugs
   and lighting zones, and the header MODE switch drives house scenes) and
   PLANNER (local day/week/month calendar + to-do list, backed by
   /api/events and /api/tasks — loaded on switch, refreshed on the 30s tick
   while open). */

"use strict";

const POLL_FAST_MS = 5000;   // latest values, plug states, ticker
const POLL_SLOW_MS = 30000;  // sparklines + open detail dialog
const SPARK_RANGE = "3h";
/* Every widget's detail chart opens on this range, every time — not just the
   first time. A range picked while looking at one metric should not silently
   become the frame you read the next one through. */
const DEFAULT_DETAIL_RANGE = "3h";

/* Phone layout. The LAYOUT is CSS's job (see the phone block at the end of
   styles.css) — this flag exists only for the handful of decisions a
   stylesheet cannot make: which calendar view opens, and whether the
   persisted calendar sizing applies at all. Keep the breakpoint in sync with
   that block. Read live rather than cached at load, so rotating a phone or
   dragging a desktop window narrow is answered correctly. */
const PHONE_QUERY = window.matchMedia("(max-width: 700px)");
function isPhone() { return PHONE_QUERY.matches; }

/* allowNegative: whether a chart's y-axis may drop below zero. Only
   temperature can legitimately be negative — a humidity, lux, CO2 or watt
   axis running into negative numbers is nonsense, so those clamp at 0. */
const METRICS = [
  { id: "temp", key: "TEMP", unit: "°C", decimals: 1, color: cssVar("--s-temp"), allowNegative: true },
  { id: "hum",  key: "HUM",  unit: "%RH", decimals: 1, color: cssVar("--s-hum") },
  { id: "lux",  key: "LUX",  unit: "lx",  decimals: 0, color: cssVar("--s-lux") },
  { id: "co2",  key: "CO2",  unit: "ppm", decimals: 0, color: cssVar("--s-co2") },
];

const POWER_COLOR = cssVar("--s-power");

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
  return resp.json();
}

async function sendJSON(method, url, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `${url} -> ${resp.status}`);
  return data;
}

const postJSON = (url, body) => sendJSON("POST", url, body);
const putJSON = (url, body) => sendJSON("PUT", url, body);
const deleteJSON = (url) => sendJSON("DELETE", url);

/* ------------------------------------------------------ alert thresholds */

let thresholds = null; // {temp: {min, max}, ..., power: {min, max}}

async function loadThresholds() {
  try {
    thresholds = await getJSON("/api/settings/thresholds");
  } catch { /* alerts stay off until the next successful load */ }
}

// -> "high" | "low" | null
function alertState(key, value) {
  const t = thresholds && thresholds[key];
  if (!t || value === null || value === undefined) return null;
  if (t.min !== null && value < t.min) return "low";
  if (t.max !== null && value > t.max) return "high";
  return null;
}

function applyAlert(widget, state) {
  const metricValue = widget.querySelector(".metric-value");
  metricValue.classList.toggle("alert", Boolean(state));
  let chip = metricValue.querySelector(".alert-chip");
  if (state) {
    if (!chip) {
      chip = document.createElement("span");
      chip.className = "alert-chip";
      metricValue.appendChild(chip);
    }
    chip.textContent = state === "high" ? "▲ HIGH" : "▼ LOW";
  } else if (chip) {
    chip.remove();
  }
}

/* ------------------------------------------------------- scenes (house modes)
   Three named modes — Sleeping / Day / Away — switched from the header, each
   setting the plug + bulb zones in one shot (see /api/scenes). While a scene
   other than Day is active the backend pauses auto lighting; the lighting
   cards say so. Sleeping opens a dialog with an optional wake time (a
   scheduled scene change back to Home — not an alarm). */

let activeScene = null; // {name, activated_at, wake_time, wake_at} from /api/scenes/active
let lastSummary = null; // /api/scenes/last-summary payload (or null)

const sceneButtons = [...document.querySelectorAll(".scene-btn")];
const sceneNote = document.getElementById("scene-note");

function renderScene() {
  const name = activeScene && activeScene.name;
  for (const btn of sceneButtons) {
    const on = btn.dataset.scene === name;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", String(on));
  }
  if (activeScene && activeScene.name === "Sleeping" && activeScene.wake_time) {
    sceneNote.textContent = `→ Home ${activeScene.wake_time}`;
    sceneNote.title = "Switches the scene back to Home at this time (not an alarm)";
  } else {
    sceneNote.textContent = "";
    sceneNote.title = "";
  }
  sceneNote.classList.remove("err");
  // Only useful while Away — everywhere else an arrival is a no-op.
  document.getElementById("im-home-btn").hidden = name !== "Away";
  renderSummaryCard();
}

/* "I'm home" — the manual fallback when the arrival Shortcut didn't fire.
   Deliberately posts to /api/presence/arrived rather than activating a scene:
   the host then picks Home or Sleeping from the nightly window, so tapping
   this at 02:00 puts the house to bed instead of switching every light on.
   Exactly the decision the phone would have triggered. */
document.getElementById("im-home-btn").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  try {
    const result = await postJSON("/api/presence/arrived");
    if (result.summary_generated) await loadAwaySummary();
    await pollFast();
    sceneNote.classList.remove("err");
  } catch {
    sceneNote.textContent = "Couldn't reach the hub.";
    sceneNote.classList.add("err");
  } finally {
    btn.disabled = false;
  }
});

async function activateScene(name, wakeTime) {
  const result = await postJSON(`/api/scenes/${encodeURIComponent(name)}/activate`,
    wakeTime ? { wake_time: wakeTime } : undefined);
  activeScene = result.active;
  renderScene();
  if (result.summary_generated) loadSummary();
  // Away -> Home/Sleeping by hand generates a disturbance summary too. Missing
  // this was why switching manually out of Away showed nothing.
  if (result.away_summary_generated) loadAwaySummary();
  pollFast(); // device widgets reflect the scene's states right away
  return result;
}

sceneButtons.forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    if (btn.dataset.scene === "Sleeping") {
      openSleepDialog(); // re-openable while Sleeping, to adjust the wake time
      return;
    }
    if (btn.classList.contains("active")) return;
    btn.disabled = true;
    try {
      await activateScene(btn.dataset.scene, null);
    } catch {
      sceneNote.textContent = "scene failed";
      sceneNote.title = "Couldn't activate the scene — is the backend up?";
      sceneNote.classList.add("err");
    } finally {
      btn.disabled = false;
    }
  });
});

/* ---------------------------------------------------------- live values */

function setLink(online) {
  const el = document.getElementById("link-status");
  el.classList.toggle("online", online);
  el.classList.toggle("offline", !online);
  document.getElementById("link-label").textContent = online ? "online" : "backend unreachable";
}

function renderTicker(latest) {
  const line = document.getElementById("serial-line");
  const parts = [];
  for (const m of [...METRICS, { id: "motion", key: "MOTION", decimals: 0 }]) {
    const r = latest[m.id];
    if (!r) continue;
    parts.push(`<span class="k">${m.key}:</span>${Number(r.value).toFixed(m.decimals)}`);
  }
  if (parts.length) line.innerHTML = parts.join('<span class="sep">·</span>');
}

function renderMetricValues(latest) {
  const now = Date.now() / 1000;
  for (const m of METRICS) {
    const widget = document.querySelector(`.widget[data-metric="${m.id}"]`);
    const r = latest[m.id];
    if (r) {
      widget.querySelector(".value").textContent = Number(r.value).toFixed(m.decimals);
      widget.querySelector(".metric-value").classList.toggle("stale", now - r.ts > 60);
      applyAlert(widget, alertState(m.id, Number(r.value)));
    }
  }
}

/* Motion means something different in each scene, so each scene shows it
   differently: suppressed while Home (you are the one moving, and the PIR
   fires more or less continuously), the primary signal while Away, and the
   awakenings count while Sleeping.

   Recording never stops — this is a presentation decision only, so "was anyone
   in the room at 15:00 last Tuesday?" stays answerable. A suppressed control
   also has to SAY it is suppressed: silently reporting "quiet" while the
   sensor is firing would be worse than the noise it replaces. Same wording as
   the auto-lighting cards' "Auto paused — ‹scene› scene active". */
function motionSuppressed() {
  return !activeScene || activeScene.name === "Home";
}

function renderPIR(latest) {
  const r = latest.motion;
  const stateEl = document.getElementById("pir-state");
  const label = document.getElementById("pir-label");
  const note = document.getElementById("pir-note");

  if (motionSuppressed()) {
    stateEl.classList.remove("active");
    label.textContent = "paused";
    note.textContent = "Motion paused — Home scene active. Still recorded, "
      + "just not shown while you are the one moving.";
    return;
  }
  if (!r) { label.textContent = "no data"; return; }
  const active = Number(r.value) === 1;
  stateEl.classList.toggle("active", active);
  label.textContent = active ? "MOTION" : "quiet";
  note.textContent = active
    ? "PIR is reporting movement right now."
    : `Last report ${relTime(r.ts)}. HC-SR501 on pin D2.`;
}

async function pollFast() {
  try {
    const [latest, devices, scene] = await Promise.all([
      getJSON("/api/sensors/latest"),
      getJSON("/api/devices"),
      getJSON("/api/scenes/active"),
    ]);
    setLink(true);
    // scene first — the lighting cards' status lines depend on it. A
    // server-side switch into Home (the wake timer firing) means a fresh
    // overnight summary is waiting.
    const prevSceneName = activeScene && activeScene.name;
    activeScene = scene;
    renderScene();
    // A server-side transition (wake timer, nightly schedule, a phone arrival)
    // may have produced either summary. Checked on the transition rather than
    // every poll.
    if (prevSceneName && prevSceneName !== scene.name) {
      if (scene.name === "Home") loadSummary();
      if (prevSceneName === "Away") loadAwaySummary();
    }
    renderTicker(latest);
    renderMetricValues(latest);
    renderPIR(latest);
    renderPlugs(devices);
    renderLighting(devices, latest);
  } catch {
    setLink(false);
  }
}

/* ----------------------------------------------------------------- plugs */

const plugPairs = new Map(); // device id -> .plug-pair element

function plugPairDOM(device) {
  const pair = document.createElement("div");
  pair.className = "plug-pair";
  pair.innerHTML = `
    <article class="card plug-control">
      <!-- Name and lock share the top row; the IP gets its own line below.
           A plug card is only ~205px wide (four across the Power zone), and an
           IPv4 address in mono is ~100px of that with no break opportunity, so
           name + IP + lock on one row cannot fit and pushed the lock outside
           the card. The lock stays in the head because it is the action. -->
      <header class="card-head">
        <h3 class="card-label"></h3>
        <button class="plug-lock-btn" type="button" aria-pressed="false"
                title="Lock to prevent power-off" disabled>
          <svg class="lock-icon-unlocked" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 7.75-1.5"></path></svg>
          <svg class="lock-icon-locked" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 8 0v4"></path></svg>
        </button>
      </header>
      <p class="plug-meta"><code class="wire-key"></code></p>
      <p class="plug-room"></p>
      <button class="plug-switch" role="switch" aria-checked="false" disabled>
        <span class="switch-track"><span class="switch-thumb"></span></span>
        <span class="switch-label">…</span>
      </button>
      <p class="card-note">Waiting for first poll.</p>
    </article>
    <article class="widget power-widget" data-widget="power" tabindex="0">
      <header class="card-head">
        <h3 class="card-label">Power draw</h3>
        <span class="head-right"><code class="wire-key">W</code><span class="expand-hint" aria-hidden="true">⤢</span></span>
      </header>
      <p class="metric-value"><span class="value">—</span><span class="unit">W</span></p>
      <div class="spark" aria-hidden="true"></div>
      <div class="widget-detail" hidden>
        <div class="detail-range" role="group" aria-label="History range">
          <button class="range-btn active" data-range="3h">3h</button>
          <button class="range-btn" data-range="24h">24h</button>
          <button class="range-btn" data-range="7d">7d</button>
        </div>
        <div class="chart chart-tall"></div>
        <dl class="stat-row">
          <div><dt>Avg 24h</dt><dd data-stat="avg_24h_w">—</dd></div>
          <div><dt>Energy 24h</dt><dd data-stat="kwh_24h">—</dd></div>
          <div><dt>Avg 7d</dt><dd data-stat="avg_7d_w">—</dd></div>
        </dl>
      </div>
    </article>`;

  // device fields are API data — set as text, never markup
  pair.querySelector(".plug-control .card-label").textContent = device.name;
  pair.querySelector(".plug-control .wire-key").textContent = device.ip || "no ip";
  pair.querySelector(".plug-room").textContent = device.room || "";

  const widget = pair.querySelector(".power-widget");
  widget.dataset.deviceId = device.id;
  widget.dataset.name = device.name;
  wireWidget(widget);

  const sw = pair.querySelector(".plug-switch");
  const note = pair.querySelector(".card-note");

  sw.addEventListener("click", async () => {
    if (sw.disabled) return;
    sw.disabled = true;
    try {
      const result = await postJSON(`/api/devices/${device.id}/toggle`);
      applySwitch(pair, Boolean(result.relay_on));
      note.textContent = result.relay_on
        ? "Turned on. Draw updates on the next poll."
        : "Turned off.";
    } catch {
      note.textContent = "Couldn't reach the plug — is it powered and on the network?";
    } finally {
      // Re-enable only if still unlocked — a lock could have landed mid-request.
      sw.disabled = pair.dataset.locked === "true";
    }
  });

  const lockBtn = pair.querySelector(".plug-lock-btn");
  lockBtn.addEventListener("click", async () => {
    if (lockBtn.disabled) return;
    if (!lockBtn.classList.contains("locked")) {
      // Arming the lock needs no ceremony — only removing it does.
      lockBtn.disabled = true;
      try {
        await postJSON(`/api/devices/${device.id}/lock`, { locked: true });
        applyLock(pair, true);
      } catch {
        note.textContent = "Couldn't lock the plug — is the backend up?";
      } finally {
        lockBtn.disabled = false;
      }
    } else {
      openUnlockDialog(device.id, device.name, pair);
    }
  });

  return pair;
}

function applySwitch(pair, on) {
  const sw = pair.querySelector(".plug-switch");
  sw.setAttribute("aria-checked", String(on));
  pair.querySelector(".switch-label").textContent = on ? "On" : "Off";
}

function applyLock(pair, locked) {
  pair.dataset.locked = String(locked);
  const btn = pair.querySelector(".plug-lock-btn");
  btn.classList.toggle("locked", locked);
  btn.setAttribute("aria-pressed", String(locked));
  btn.title = locked ? "Locked — click to unlock before switching" : "Lock to prevent power-off";
  // Apply immediately (not just on the next poll) so arming/unlocking feels
  // instant, same fix as the lighting mode-toggle latency bug — but only
  // once we've actually polled once, so "waiting for first poll" still holds.
  if (pair.dataset.polled === "true") {
    pair.querySelector(".plug-switch").disabled = locked;
  }
}

function renderPlugs(devices) {
  const list = document.getElementById("plug-list");
  const empty = document.getElementById("plug-empty");
  for (const device of devices) {
    if (device.type !== "wifi_plug") continue;
    if (empty) empty.remove();
    let pair = plugPairs.get(device.id);
    if (!pair) {
      pair = plugPairDOM(device);
      plugPairs.set(device.id, pair);
      list.appendChild(pair);
      refreshSpark(pair.querySelector(".power-widget"));
    }
    applyLock(pair, Boolean(device.locked));
    pair.querySelector(".plug-lock-btn").disabled = false;

    const power = device.power;
    if (!power) continue;
    pair.dataset.polled = "true";
    applySwitch(pair, power.relay_on === 1);
    pair.querySelector(".plug-switch").disabled = Boolean(device.locked);
    pair.querySelector(".power-widget .value").textContent =
      power.watts === null ? "—" : Number(power.watts).toFixed(1);
    applyAlert(pair.querySelector(".power-widget"),
      power.watts === null ? null : alertState("power", Number(power.watts)));
    pair.querySelector(".card-note").textContent = `Polled ${relTime(power.ts)}.`;
  }
}

/* -------------------------------------------------------------- lighting
   One .light-card per bulb_zone device (a Shelly Multicolor Bulb E27 Gen3).
   The top-right switch is always the zone's physical on/off; Mode
   (manual/auto) lives in the control rows with brightness/color. In 'auto'
   mode the lighting job (app/lighting.py) drives on/brightness from lux, so
   brightness goes read-only and just keeps reflecting the live value each
   poll — it isn't hidden, since color stays user-editable in either mode.
   Brightness is 0-255 here and everywhere else in the hub; the bulb's
   1-100 % scale is converted server-side in app/shelly_bulb.py. */

const lightCards = new Map(); // device id -> .light-card element
const WARMTH_DEFAULT_K = 2700; // typical warm-white home bulb
/* Hard limits of the bulb's white channel, not a taste choice: it answers a
   `ct` outside 2700-6500 with JSON-RPC error -103 rather than clamping, so a
   wider slider would simply fail on every send below 2700. The old 1800 K
   "candle flame" floor was reachable only because the ambient control used to
   fake warm white out of the RGB dies — which is exactly what made it dim. */
const WARMTH_MIN_K = 2700;     // bulb's warmest true white
const WARMTH_MAX_K = 6500;     // crisp daylight white

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex([r, g, b]) {
  return "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
}

function clampK(kelvin) {
  return Math.max(WARMTH_MIN_K, Math.min(WARMTH_MAX_K, Math.round(kelvin)));
}

/* The ambient ramp — now PREVIEW ONLY.

   The ambient control no longer sends RGB at all. It sends a `ct` in kelvin
   and the bulb lights its dedicated white channel, which is where nearly all
   of its output lives (measured at full brightness on the real bulb: cct
   2700K = 8.7W, the equivalent RGB warm white = 5.5W, and the lumen gap is
   wider still because RGB dies are far less efficacious per watt). Faking warm
   white from the colour dies is what made ambient lighting dim.

   These values now only tint the swatch and the slider track, so the user can
   see roughly what a given kelvin looks like. They are deliberately NOT the
   black-body/Planckian values every Kelvin-to-RGB tool returns: those are
   calibrated for a screen's D65 white point and, rendered as a small UI
   swatch, read colder than the warm light the bulb actually emits. Holding R
   at full with B below G keeps the preview honest about the warmth.

   Nothing here reaches the hardware any more, so a change to these values
   cannot affect what the bulb does — only how the control looks. */
const WARMTH_RAMP = [
  [2700, [255, 147, 55]],    // warm white (tungsten)
  [3200, [255, 165, 80]],
  [4000, [255, 190, 120]],
  [5000, [255, 212, 165]],
  [6500, [255, 228, 206]],   // neutral white
];

function kelvinToRgb(kelvin) {
  const k = Math.max(WARMTH_MIN_K, Math.min(WARMTH_MAX_K, kelvin));
  let lo = WARMTH_RAMP[0];
  let hi = WARMTH_RAMP[WARMTH_RAMP.length - 1];
  for (let i = 0; i < WARMTH_RAMP.length - 1; i++) {
    if (k >= WARMTH_RAMP[i][0] && k <= WARMTH_RAMP[i + 1][0]) {
      lo = WARMTH_RAMP[i];
      hi = WARMTH_RAMP[i + 1];
      break;
    }
  }
  const span = hi[0] - lo[0];
  const t = span === 0 ? 0 : (k - lo[0]) / span;
  return lo[1].map((v, i) => Math.round(v + (hi[1][i] - v) * t));
}

/* The slider track paints the same ramp it sends, so the gradient can't drift
   from the colors — it used to end in a blue that no Kelvin on the slider
   actually produced. A custom property because ::-moz-range-track and
   ::-webkit-slider-runnable-track can't be styled inline. */
function warmthGradientCss() {
  const stops = WARMTH_RAMP.map(([k, rgb]) => {
    const pct = ((k - WARMTH_MIN_K) / (WARMTH_MAX_K - WARMTH_MIN_K)) * 100;
    return `${rgbToHex(rgb)} ${pct.toFixed(1)}%`;
  });
  return `linear-gradient(to right, ${stops.join(", ")})`;
}
document.documentElement.style.setProperty("--warmth-gradient", warmthGradientCss());

function lightCardDOM(device) {
  const card = document.createElement("article");
  card.className = "card light-card";
  card.innerHTML = `
    <header class="card-head">
      <h3 class="card-label"></h3>
      <span class="head-right">
        <code class="wire-key"></code>
        <button class="light-power" role="switch" aria-checked="false" disabled>
          <span class="switch-track"><span class="switch-thumb"></span></span>
          <span class="switch-label">…</span>
        </button>
      </span>
    </header>
    <p class="light-room"></p>
    <div class="light-controls">
      <label class="light-field">
        <span class="light-field-label">Mode</span>
        <button class="mode-toggle" role="switch" aria-checked="false" disabled>
          <span class="switch-track"><span class="switch-thumb"></span></span>
          <span class="switch-label">…</span>
        </button>
      </label>
      <label class="light-field">
        <span class="light-field-label">Brightness</span>
        <input type="range" min="0" max="255" class="light-brightness" disabled>
        <span class="light-brightness-value">—</span>
      </label>
      <div class="light-field">
        <span class="light-field-label">Color</span>
        <div class="light-color-mode" role="group" aria-label="Color mode">
          <button type="button" class="color-mode-btn active" data-mode="ambient" disabled>Ambient</button>
          <button type="button" class="color-mode-btn" data-mode="rgb" disabled>Custom</button>
        </div>
        <span class="light-color-swatch" aria-hidden="true"></span>
      </div>
      <div class="light-field light-warmth-field">
        <span class="light-field-label"></span>
        <input type="range" min="${WARMTH_MIN_K}" max="${WARMTH_MAX_K}" step="50" value="${WARMTH_DEFAULT_K}" class="light-warmth" disabled>
        <span class="light-warmth-value">${WARMTH_DEFAULT_K}K</span>
      </div>
      <label class="light-field light-rgb-field" hidden>
        <span class="light-field-label"></span>
        <input type="color" class="light-color" disabled>
      </label>
    </div>
    <p class="card-note light-status"></p>`;

  // device fields are API data — set as text, never markup
  card.querySelector(".card-label").textContent = device.name;
  card.querySelector(".wire-key").textContent = device.ip || "no ip";
  card.querySelector(".light-room").textContent = device.room || "";
  card.querySelector(".light-color-swatch").style.background = rgbToHex(kelvinToRgb(WARMTH_DEFAULT_K));

  const status = card.querySelector(".light-status");
  const swatch = card.querySelector(".light-color-swatch");

  const modeButtons = card.querySelectorAll(".color-mode-btn");
  modeButtons.forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      modeButtons.forEach((b) => b.classList.toggle("active", b === btn));
      const isAmbient = btn.dataset.mode === "ambient";
      card.querySelector(".light-warmth-field").hidden = !isAmbient;
      card.querySelector(".light-rgb-field").hidden = isAmbient;
      // Switch the bulb's channel straight away, rather than waiting for the
      // user to also nudge a slider. Without this the bulb stays on the other
      // channel and the next poll snaps the panel back to it.
      const body = isAmbient
        ? { ct: clampK(Number(card.querySelector(".light-warmth").value)) }
        : { color: hexToRgb(card.querySelector(".light-color").value) };
      try {
        applyLightState(card, await postJSON(`/api/devices/${device.id}/state`, body));
        status.textContent = "";
      } catch {
        status.textContent = "Couldn't reach the zone.";
      }
    });
  });

  const modeToggle = card.querySelector(".mode-toggle");
  modeToggle.addEventListener("click", async () => {
    if (modeToggle.disabled) return;
    modeToggle.disabled = true;
    const newMode = modeToggle.getAttribute("aria-checked") === "true" ? "manual" : "auto";
    try {
      await postJSON(`/api/devices/${device.id}/mode`, { mode: newMode });
      applyLightMode(card, newMode);
    } catch {
      status.textContent = "Couldn't change mode — is the zone reachable?";
    } finally {
      modeToggle.disabled = false;
    }
  });

  const powerSwitch = card.querySelector(".light-power");
  powerSwitch.addEventListener("click", async () => {
    if (powerSwitch.disabled) return;
    powerSwitch.disabled = true;
    const on = powerSwitch.getAttribute("aria-checked") !== "true";
    try {
      applyLightState(card, await postJSON(`/api/devices/${device.id}/state`, { on }));
      status.textContent = "";
    } catch {
      status.textContent = "Couldn't reach the zone.";
    } finally {
      powerSwitch.disabled = false;
    }
  });

  const brightness = card.querySelector(".light-brightness");
  brightness.addEventListener("input", () => {
    card.querySelector(".light-brightness-value").textContent = brightness.value;
  });
  brightness.addEventListener("change", async () => {
    try {
      applyLightState(card, await postJSON(`/api/devices/${device.id}/state`,
        { brightness: Number(brightness.value) }));
      status.textContent = "";
    } catch {
      status.textContent = "Couldn't reach the zone.";
    }
  });

  const warmth = card.querySelector(".light-warmth");
  const warmthValue = card.querySelector(".light-warmth-value");
  warmth.addEventListener("input", () => {
    warmthValue.textContent = `${warmth.value}K`;
    swatch.style.background = rgbToHex(kelvinToRgb(Number(warmth.value)));
  });
  warmth.addEventListener("change", async () => {
    try {
      // Sends `ct`, never rgb: this drives the bulb's white channel, which is
      // both the correct rendering of "warm white" and far brighter than
      // mixing the same colour out of the RGB dies.
      applyLightState(card, await postJSON(`/api/devices/${device.id}/state`,
        { ct: Number(warmth.value) }));
      status.textContent = "";
    } catch {
      status.textContent = "Couldn't reach the zone.";
    }
  });

  const color = card.querySelector(".light-color");
  color.addEventListener("input", () => {
    swatch.style.background = color.value;
  });
  color.addEventListener("change", async () => {
    try {
      applyLightState(card, await postJSON(`/api/devices/${device.id}/state`,
        { color: hexToRgb(color.value) }));
      status.textContent = "";
    } catch {
      status.textContent = "Couldn't reach the zone.";
    }
  });

  return card;
}

function applyLightMode(card, mode) {
  const isAuto = mode === "auto";
  card.dataset.mode = mode;
  const toggle = card.querySelector(".mode-toggle");
  toggle.setAttribute("aria-checked", String(isAuto));
  toggle.querySelector(".switch-label").textContent = isAuto ? "Auto" : "Manual";
  // Brightness is disabled by mode as much as by reachability, so apply that
  // here (not just in renderLighting) — a mode-toggle click needs this to
  // take effect immediately, not wait for the next 5s poll to unlock it.
  // On/off stays independent of mode — it's gated by reachability only, so
  // the zone can always be switched off even while the lighting job is
  // driving its brightness (the next auto tick may reassert brightness, but
  // never fights an explicit on/off click).
  const reachable = card.dataset.reachable === "true";
  card.querySelector(".light-power").disabled = !reachable;
  card.querySelector(".light-brightness").disabled = isAuto || !reachable;
}

function applyLightState(card, state) {
  if (!state) return;
  const power = card.querySelector(".light-power");
  power.setAttribute("aria-checked", String(Boolean(state.on)));
  power.querySelector(".switch-label").textContent = state.on ? "On" : "Off";
  const brightness = card.querySelector(".light-brightness");
  brightness.value = state.brightness ?? 0;
  card.querySelector(".light-brightness-value").textContent = brightness.value;
  if (state.color) {
    card.querySelector(".light-color").value = rgbToHex(state.color);
  }
  if (state.ct) {
    const warmth = card.querySelector(".light-warmth");
    warmth.value = clampK(state.ct);
    card.querySelector(".light-warmth-value").textContent = `${warmth.value}K`;
  }
  // The bulb lights one channel at a time, so follow whichever it reports —
  // otherwise a change made in the Shelly app (or by a scene) leaves the card
  // showing the wrong control and the swatch showing a colour that is not lit.
  const ambient = (state.color_mode || "cct") !== "rgb";
  card.querySelectorAll(".color-mode-btn").forEach((b) => {
    b.classList.toggle("active", (b.dataset.mode === "ambient") === ambient);
  });
  card.querySelector(".light-warmth-field").hidden = !ambient;
  card.querySelector(".light-rgb-field").hidden = ambient;
  card.querySelector(".light-color-swatch").style.background = ambient
    ? rgbToHex(kelvinToRgb(Number(card.querySelector(".light-warmth").value)))
    : rgbToHex(state.color || [255, 176, 102]);
}

function renderLighting(devices, latest) {
  const list = document.getElementById("lighting-list");
  const empty = document.getElementById("lighting-empty");
  const lux = latest && latest.lux;
  for (const device of devices) {
    if (device.type !== "bulb_zone") continue;
    if (empty) empty.remove();
    let card = lightCards.get(device.id);
    if (!card) {
      card = lightCardDOM(device);
      lightCards.set(device.id, card);
      list.appendChild(card);
    }
    const mode = device.mode || "manual";
    const isAuto = mode === "auto";
    card.dataset.reachable = String(Boolean(device.light));
    applyLightMode(card, mode); // also (re)applies power/brightness disabled state
    card.querySelector(".mode-toggle").disabled = false;

    const status = card.querySelector(".light-status");
    const color = card.querySelector(".light-color");
    const warmth = card.querySelector(".light-warmth");
    const colorModeBtns = card.querySelectorAll(".color-mode-btn");

    if (device.light) {
      // brightness (and on/off) keep reflecting the live value every poll,
      // whether a manual edit or the auto job's last tick set it
      applyLightState(card, device.light);
      color.disabled = false;
      warmth.disabled = false;
      colorModeBtns.forEach((b) => { b.disabled = false; });
      const suppressedBy = activeScene && activeScene.name !== "Home" ? activeScene.name : null;
      /* In auto mode the backend's controller already knows whether it is
         holding the target, still converging, or saturated — "the room is
         already brighter than the target" is a normal outcome and the card has
         to say so, otherwise auto mode just looks broken. device.auto.detail
         is that sentence; fall back to the raw lux if an older backend or a
         tick that hasn't run yet leaves it absent. */
      status.textContent = !isAuto
        ? ""
        : suppressedBy
          ? `Auto paused — ${suppressedBy} scene active.`
          : device.auto && device.auto.detail
            ? `Auto — ${device.auto.detail}`
            : `Auto — following ambient light${lux ? ` (${Number(lux.value).toFixed(0)} lx)` : ""}.`;
      status.classList.toggle(
        "light-status-capped",
        Boolean(isAuto && !suppressedBy && device.auto
                && (device.auto.state === "too_bright" || device.auto.state === "at_max")));
    } else {
      color.disabled = true;
      warmth.disabled = true;
      colorModeBtns.forEach((b) => { b.disabled = true; });
      status.textContent = "Zone unreachable — is it powered and on the network?";
    }
  }
}

/* --------------------------------------------------- detail overlay dialog
   The clicked widget's .widget-detail node is moved into the dialog and
   moved back on close — the board itself never reflows. */

const overlayEl = document.getElementById("detail-overlay");
const detailBody = document.getElementById("detail-body");
let openWidget = null;

function wireWidget(widget) {
  widget._detail = widget.querySelector(".widget-detail");
  widget.addEventListener("click", () => openDetail(widget));
  widget.addEventListener("keydown", (ev) => {
    if ((ev.key === "Enter" || ev.key === " ") && ev.target === widget) {
      ev.preventDefault();
      openDetail(widget);
    }
  });
  widget._detail.querySelectorAll(".detail-range .range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      widget._detail.querySelectorAll(".detail-range .range-btn").forEach((b) =>
        b.classList.toggle("active", b === btn));
      widget.dataset.range = btn.dataset.range;
      loadDetail(widget);
    });
  });
}

function openDetail(widget) {
  if (openWidget) closeDetail();
  openWidget = widget;
  // Always open on the default range, never on whatever was picked last time.
  // A 7d frame chosen while looking at CO2 should not silently become how you
  // read temperature ten minutes later — the axis label is easy to miss, and
  // misreading a week as a day is worse than one extra click.
  const rangeGroup = widget._detail.querySelector(".detail-range");
  if (rangeGroup) {
    widget.dataset.range = DEFAULT_DETAIL_RANGE;
    rangeGroup.querySelectorAll(".range-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.range === DEFAULT_DETAIL_RANGE));
  }
  document.getElementById("detail-title").textContent =
    widget.dataset.name || widget.querySelector(".card-label").textContent;
  document.getElementById("detail-key").textContent =
    widget.querySelector(".wire-key").textContent;
  detailBody.appendChild(widget._detail);
  widget._detail.hidden = false;
  overlayEl.hidden = false;
  document.body.style.overflow = "hidden";
  loadDetail(widget);
  document.getElementById("detail-close").focus();
}

function closeDetail() {
  if (!openWidget) return;
  const sel = document.getElementById("chart-selection");
  if (sel) sel.hidden = true;
  openWidget._detail.hidden = true;
  openWidget.appendChild(openWidget._detail);
  overlayEl.hidden = true;
  document.body.style.overflow = "";
  openWidget.focus();
  openWidget = null;
}

document.getElementById("detail-close").addEventListener("click", closeDetail);
document.getElementById("detail-backdrop").addEventListener("click", closeDetail);
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if (!unlockOverlay.hidden) closeUnlockDialog();
  else if (!sleepOverlay.hidden) closeSleepDialog();
  else if (!settingsOverlay.hidden) closeSettings();
  else if (!eventOverlay.hidden) closeEventDialog();
  else if (!vitalsOverlay.hidden) closeVitalsDetail();
  else if (!sleepdOverlay.hidden) closeSleepDetail();
  else if (!overlayEl.hidden) closeDetail();
});

/* -------------------------------------------------------- settings dialog */

const settingsOverlay = document.getElementById("settings-overlay");
const settingsForm = document.getElementById("settings-form");
const saveNote = document.getElementById("save-note");
const targetLuxInput = document.getElementById("target-lux-input");
const sleepEnabledInput = document.getElementById("sleep-enabled-input");
const sleepFromInput = document.getElementById("sleep-from-input");
const sleepToInput = document.getElementById("sleep-to-input");

// data-key marks the threshold inputs specifically — the dialog also holds the
// auto-lighting and nightly-sleep fields, which post to their own endpoints.
const thresholdInputs = () => settingsForm.querySelectorAll(".threshold-row input[data-key]");

async function openSettings() {
  if (thresholds) {
    thresholdInputs().forEach((input) => {
      const t = thresholds[input.dataset.key];
      const v = t ? t[input.dataset.bound] : null;
      input.value = v === null || v === undefined ? "" : v;
    });
  }
  saveNote.textContent = "";
  saveNote.className = "save-note";
  settingsOverlay.hidden = false;
  document.body.style.overflow = "hidden";
  settingsForm.querySelector("input").focus();
  // Fetched on open rather than kept in the poll loop — neither changes
  // except from this dialog, so there's nothing to keep live.
  try {
    const [lighting, schedule] = await Promise.all([
      getJSON("/api/settings/lighting"),
      getJSON("/api/settings/sleep-schedule"),
    ]);
    targetLuxInput.value = lighting.target_lux;
    sleepEnabledInput.checked = !!schedule.enabled;
    sleepFromInput.value = schedule.sleep_time;
    sleepToInput.value = schedule.wake_time;
  } catch {
    saveNote.textContent = "Couldn't load lighting/sleep settings.";
    saveNote.className = "save-note err";
  }
}

function closeSettings() {
  settingsOverlay.hidden = true;
  if (overlayEl.hidden && unlockOverlay.hidden && sleepOverlay.hidden && eventOverlay.hidden) {
    document.body.style.overflow = "";
  }
  document.getElementById("settings-btn").focus();
}

document.getElementById("settings-btn").addEventListener("click", openSettings);
document.getElementById("settings-close").addEventListener("click", closeSettings);
document.getElementById("settings-backdrop").addEventListener("click", closeSettings);

settingsForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const thresholdBody = {};
  thresholdInputs().forEach((input) => {
    const key = input.dataset.key;
    thresholdBody[key] = thresholdBody[key] || {};
    thresholdBody[key][input.dataset.bound] =
      input.value.trim() === "" ? null : Number(input.value);
  });
  try {
    // Sequential, not Promise.all: if one is rejected the note should name
    // that failure, and a half-saved dialog is easier to reason about when
    // the sections apply in the order they're shown.
    thresholds = await putJSON("/api/settings/thresholds", thresholdBody);
    await putJSON("/api/settings/lighting",
      { target_lux: targetLuxInput.value.trim() === "" ? null : Number(targetLuxInput.value) });
    await putJSON("/api/settings/sleep-schedule", {
      enabled: sleepEnabledInput.checked,
      sleep_time: sleepFromInput.value,
      wake_time: sleepToInput.value,
    });
    saveNote.textContent = "Saved.";
    saveNote.className = "save-note ok";
    pollFast(); // re-evaluate widget alerts right away
    if (openWidget) loadDetail(openWidget); // redraw threshold lines
  } catch (err) {
    saveNote.textContent = err.message || "Couldn't save — is the backend up?";
    saveNote.className = "save-note err";
  }
});

/* -------------------------------------------------- plug unlock dialog
   A locked plug's switch is fully disabled (neither on nor off) until this
   typed confirmation clears the lock. Arming the lock (see applyLock's
   caller) needs no ceremony — only removing it does. */

const unlockOverlay = document.getElementById("unlock-overlay");
const unlockForm = document.getElementById("unlock-form");
const unlockInput = document.getElementById("unlock-input");
const unlockNote = document.getElementById("unlock-note");
let unlockTarget = null; // { deviceId, pair } for the plug being unlocked

function openUnlockDialog(deviceId, deviceName, pair) {
  unlockTarget = { deviceId, pair };
  document.getElementById("unlock-title").textContent = `Unlock ${deviceName}`;
  unlockInput.value = "";
  unlockNote.textContent = "";
  unlockNote.className = "save-note";
  unlockOverlay.hidden = false;
  document.body.style.overflow = "hidden";
  unlockInput.focus();
}

function closeUnlockDialog() {
  unlockOverlay.hidden = true;
  if (overlayEl.hidden && settingsOverlay.hidden && sleepOverlay.hidden && eventOverlay.hidden) {
    document.body.style.overflow = "";
  }
  unlockTarget = null;
}

document.getElementById("unlock-close").addEventListener("click", closeUnlockDialog);
document.getElementById("unlock-backdrop").addEventListener("click", closeUnlockDialog);

unlockForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!unlockTarget) return;
  if (unlockInput.value.trim().toLowerCase() !== "unlock") {
    unlockInput.classList.add("shake");
    setTimeout(() => unlockInput.classList.remove("shake"), 300);
    unlockNote.textContent = "Type UNLOCK exactly to confirm.";
    unlockNote.className = "save-note err";
    return;
  }
  const { deviceId, pair } = unlockTarget;
  try {
    await postJSON(`/api/devices/${deviceId}/lock`, { locked: false, confirm: "unlock" });
    applyLock(pair, false);
    closeUnlockDialog();
  } catch (err) {
    unlockNote.textContent = err.message || "Couldn't unlock — is the backend up?";
    unlockNote.className = "save-note err";
  }
});

/* ------------------------------------------------- sleeping-scene dialog
   Activation plus the optional wake time (a scheduled scene change back to
   Day — explicitly not an alarm). Blank time = stay in Sleeping until the
   mode is changed by hand. */

const sleepOverlay = document.getElementById("sleep-overlay");
const sleepForm = document.getElementById("sleep-form");
const wakeInput = document.getElementById("wake-time-input");
const sleepNote = document.getElementById("sleep-note");

function openSleepDialog() {
  // pre-fill the current wake time when adjusting an active Sleeping scene
  wakeInput.value =
    (activeScene && activeScene.name === "Sleeping" && activeScene.wake_time) || "";
  sleepNote.textContent = "";
  sleepNote.className = "save-note";
  sleepOverlay.hidden = false;
  document.body.style.overflow = "hidden";
  wakeInput.focus();
}

function closeSleepDialog() {
  sleepOverlay.hidden = true;
  if (overlayEl.hidden && settingsOverlay.hidden && unlockOverlay.hidden && eventOverlay.hidden) {
    document.body.style.overflow = "";
  }
}

document.getElementById("sleep-close").addEventListener("click", closeSleepDialog);
document.getElementById("sleep-backdrop").addEventListener("click", closeSleepDialog);

sleepForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await activateScene("Sleeping", wakeInput.value || null);
    closeSleepDialog();
  } catch (err) {
    sleepNote.textContent = err.message || "Couldn't activate — is the backend up?";
    sleepNote.className = "save-note err";
  }
});

/* ------------------------------------------------------ away summary card
   What happened while the house was empty. Unlike the overnight card this one
   shows in any scene: arriving at 02:00 into Sleeping should still leave it
   waiting for you in the morning. Dismissal is per-summary in localStorage,
   keyed by the window's end timestamp. */

const awayCard = document.getElementById("away-summary-card");
const AWAY_DISMISS_KEY = "hub-away-summary-dismissed";
let lastAwaySummary = null;

async function loadAwaySummary() {
  try {
    lastAwaySummary = (await getJSON("/api/scenes/last-away-summary")).summary;
  } catch {
    return; // keep whatever we had; next poll retries
  }
  renderAwaySummaryCard();
}

function renderAwaySummaryCard() {
  const s = lastAwaySummary;
  const show = s && localStorage.getItem(AWAY_DISMISS_KEY) !== String(s.to);
  if (!show) {
    awayCard.hidden = true;
    return;
  }

  const hours = s.duration_s / 3600;
  const span = hours >= 1 ? `${hours.toFixed(1)} h` : `${Math.round(s.duration_s / 60)} min`;
  document.getElementById("away-summary-window").textContent =
    `Away ${axisTime(s.from, 0)} → ${axisTime(s.to, 0)} · ${span}`;

  // "Nothing happened" has to read as clearly as the alarming case, so it is
  // stated outright rather than left as an absence of rows.
  const verdict = document.getElementById("away-summary-verdict");
  verdict.textContent = s.disturbed
    ? "Something moved while you were out."
    : "Nothing stirred.";
  verdict.classList.toggle("alert", !!s.disturbed);
  document.getElementById("away-summary-flag").textContent = s.disturbed ? "ALERT" : "QUIET";

  // Timeline: where in the absence things happened. A tick per movement event,
  // positioned by its fraction through the window.
  const timeline = document.getElementById("away-timeline");
  const track = document.getElementById("away-track");
  track.textContent = "";
  const windowSpan = s.to - s.from;
  if (windowSpan > 0) {
    for (const t of (s.motion.times || [])) {
      const tick = document.createElement("span");
      tick.className = "away-tick";
      tick.style.left = `${Math.min(100, Math.max(0, ((t - s.from) / windowSpan) * 100))}%`;
      tick.title = `Movement at ${axisTime(t, 0)}`;
      track.appendChild(tick);
    }
    document.getElementById("away-axis-from").textContent = axisTime(s.from, 0);
    document.getElementById("away-axis-to").textContent = axisTime(s.to, 0);
    timeline.hidden = false;
  } else {
    timeline.hidden = true;
  }

  const stats = document.getElementById("away-summary-stats");
  stats.textContent = "";

  const m = s.motion;
  const motionSub = m.events === 0 ? "no movement"
    : `first at ${axisTime(m.times[0], 0)}`;
  stats.append(summaryStat("Movement", m.events, m.events === 1 ? "event" : "events",
                           motionSub, m.events > 0));

  // Omitted rather than zeroed when no valid CO2 exists in the window — a dead
  // sensor must never render as "all clear".
  if (s.co2) {
    stats.append(summaryStat("CO₂ peak", s.co2.max, "ppm",
                             s.co2.delta === null ? null : `${s.co2.start} → ${s.co2.end} ppm`,
                             s.co2.rose_significantly));
  }
  if (s.lux && s.lux.max !== null) {
    stats.append(summaryStat("Light peak", s.lux.max, "lx", null, false));
  }
  const changed = (s.plugs || []).filter((p) => p.changed);
  if (s.plugs && s.plugs.length) {
    stats.append(summaryStat("Plugs", changed.length ? changed.length : "—",
                             changed.length ? "changed" : null,
                             changed.length ? changed.map((p) => p.name).join(", ")
                                            : "unchanged",
                             changed.length > 0));
  }

  // Ambient conditions as one quiet line, not stat tiles — this card is about
  // disturbances, and giving temperature equal visual weight would bury them.
  const bits = [];
  if (s.temp && s.temp.avg !== null) bits.push(`${s.temp.avg} °C avg (${s.temp.min}–${s.temp.max})`);
  if (s.hum && s.hum.avg !== null) bits.push(`${s.hum.avg} %RH avg`);
  if (s.lux && s.lux.max !== null) bits.push(`peak ${s.lux.max} lx`);
  document.getElementById("away-ambient").textContent =
    bits.length ? `Room while out: ${bits.join(" · ")}` : "";

  awayCard.hidden = false;
}

document.getElementById("away-summary-dismiss").addEventListener("click", () => {
  if (lastAwaySummary) localStorage.setItem(AWAY_DISMISS_KEY, String(lastAwaySummary.to));
  awayCard.hidden = true;
});

/* ------------------------------------------------- overnight summary card
   Shown in Room conditions while the house is in Day and the latest
   Sleeping -> Day summary hasn't been dismissed (dismissal is remembered
   per-summary in localStorage, keyed by its end timestamp). */

const summaryCard = document.getElementById("summary-card");
const SUMMARY_DISMISS_KEY = "hub-summary-dismissed";

async function loadSummary() {
  try {
    lastSummary = (await getJSON("/api/scenes/last-summary")).summary;
  } catch {
    return; // keep whatever we had; next poll retries
  }
  renderSummaryCard();
}

// one <div><dt><dd(+unit)>[<sub>]</div> cell of the summary stat row
function summaryStat(label, value, unit, sub, subAlert) {
  const wrap = document.createElement("div");
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value === null || value === undefined ? "—" : String(value);
  if (unit && value !== null && value !== undefined) {
    const u = document.createElement("span");
    u.className = "unit";
    u.textContent = unit;
    dd.appendChild(u);
  }
  wrap.append(dt, dd);
  if (sub) {
    const sb = document.createElement("div");
    sb.className = "stat-sub" + (subAlert ? " alert" : "");
    sb.textContent = sub;
    wrap.appendChild(sb);
  }
  return wrap;
}

function renderSummaryCard() {
  const s = lastSummary;
  // Shown in ANY scene, not just Home. The two summaries are independent
  // records and each stays until dismissed or replaced by a newer one: come
  // home late and go straight to bed, and next morning you want both — the
  // away card covering the evening you were out, the overnight card covering
  // the night that followed.
  const show = s && localStorage.getItem(SUMMARY_DISMISS_KEY) !== String(s.to);
  if (!show) {
    summaryCard.hidden = true;
    return;
  }

  const hours = (s.to - s.from) / 3600;
  document.getElementById("summary-window").textContent =
    `Sleeping ${axisTime(s.from, 0)} → ${axisTime(s.to, 0)} · ${hours.toFixed(1)} h`;

  const range = (st) =>
    st.min === null ? null : `min ${st.min} · max ${st.max}`;
  // headline the overnight average (an absolute ppm level); the start→end
  // trend and the ventilate flag live on the sub-line
  const co2 = s.co2;
  const co2Sub = co2.start === null ? null
    : `${co2.start} → ${co2.end} ppm${co2.rose_significantly ? " · ventilate" : ""}`;
  const motionSub = s.motion.count === 0 ? "slept through"
    : s.motion.events.length ? `first at ${axisTime(s.motion.events[0], 0)}` : null;

  const stats = document.getElementById("summary-stats");
  stats.textContent = "";
  stats.append(
    summaryStat("Temp avg", s.temp.avg, "°C", range(s.temp), false),
    summaryStat("Humidity avg", s.hum.avg, "%RH", range(s.hum), false),
    summaryStat("CO₂ avg", co2.avg, "ppm", co2Sub, co2.rose_significantly),
    summaryStat("Got up", s.motion.count, s.motion.count === 1 ? "time" : "times", motionSub, false),
  );
  renderSummaryHealth(s.health);
  renderSummaryPlanner(s.planner);
  summaryCard.hidden = false;
}

// health half of the morning digest: last night's recovery + sleep scores.
// Absent on summaries stored before the health module existed -> stays hidden.
function renderSummaryHealth(health) {
  const box = document.getElementById("summary-health");
  box.textContent = "";
  const scores = health && (health.recovery || health.sleep);
  box.hidden = !scores;
  if (!scores) return;
  const head = document.createElement("h4");
  head.textContent = "Body";
  box.appendChild(head);
  const row = document.createElement("div");
  row.className = "summary-scores";
  if (health.recovery && health.recovery.score != null) {
    row.appendChild(summaryScoreChip("Recovery", health.recovery.score, health.recovery.provisional));
  }
  if (health.sleep && health.sleep.score != null) {
    row.appendChild(summaryScoreChip("Sleep", health.sleep.score, health.sleep.provisional));
  }
  box.appendChild(row);
  if (health.sleep && health.sleep.target_sleep_min != null) {
    box.appendChild(summaryLine(null, "",
      `Target sleep tonight ${fmtDur(health.sleep.target_sleep_min)}` +
      (health.sleep.sleep_debt_min ? ` · debt ${fmtDur(health.sleep.sleep_debt_min)}` : "") + "."));
  }
}

function summaryScoreChip(label, value, provisional) {
  const chip = document.createElement("span");
  chip.className = "summary-score";
  const num = document.createElement("b");
  num.textContent = Math.round(value);
  num.style.color = scoreColor(value);
  chip.append(`${label} `, num);
  if (provisional) chip.append(" ~");
  return chip;
}

// one .summary-line row; the leading tag (a time, or "overdue"/"high") gets
// its own styling class
function summaryLine(tag, tagClass, text) {
  const line = document.createElement("p");
  line.className = "summary-line";
  if (tag) {
    const span = document.createElement("span");
    span.className = tagClass;
    span.textContent = tag;
    line.appendChild(span);
  } else {
    line.classList.add("quiet");
  }
  line.appendChild(document.createTextNode(text));
  return line;
}

// planner half of the same card: today's events + tasks needing attention.
// Summaries stored before the planner existed have no planner key — the
// section just stays hidden for those.
function renderSummaryPlanner(planner) {
  const box = document.getElementById("summary-planner");
  box.textContent = "";
  box.hidden = !planner;
  if (!planner) return;

  const events = document.createElement("div");
  const eventsHead = document.createElement("h4");
  eventsHead.textContent = "Today";
  events.appendChild(eventsHead);
  if (!planner.events.length) {
    events.appendChild(summaryLine(null, "", "Nothing on the calendar."));
  }
  for (const ev of planner.events) {
    events.appendChild(summaryLine(eventTimeLabel(ev), "summary-time", ev.title));
  }

  const tasks = document.createElement("div");
  const tasksHead = document.createElement("h4");
  tasksHead.textContent = "Needs attention";
  tasks.appendChild(tasksHead);
  if (!planner.tasks.length) {
    tasks.appendChild(summaryLine(null, "", "No overdue or high-priority tasks."));
  }
  for (const t of planner.tasks) {
    const days = t.overdue ? daysOverdue(t.due_date) : 0;
    const tag = t.overdue
      ? (days === 1 ? "1 day overdue" : `${days} days overdue`)
      : "high priority";
    tasks.appendChild(summaryLine(tag, t.overdue ? "summary-time summary-flag" : "summary-time", t.title));
  }

  box.append(events, tasks);
}

document.getElementById("summary-dismiss").addEventListener("click", () => {
  if (lastSummary) localStorage.setItem(SUMMARY_DISMISS_KEY, String(lastSummary.to));
  summaryCard.hidden = true;
});

function setStat(widget, name, value, unit) {
  const dd = widget._detail.querySelector(`dd[data-stat="${name}"]`);
  if (!dd) return;
  dd.textContent = value === null || value === undefined ? "—" : String(value);
  if (unit && value !== null && value !== undefined) {
    const u = document.createElement("span");
    u.className = "unit";
    u.textContent = unit;
    dd.appendChild(u);
  }
}

function secondsSinceMidnight() {
  const d = new Date();
  return d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds();
}

async function loadDetail(widget) {
  const range = widget.dataset.range || DEFAULT_DETAIL_RANGE;
  const kind = widget.dataset.widget;
  const detail = widget._detail;
  try {
    if (kind === "metric") {
      const m = METRICS.find((x) => x.id === widget.dataset.metric);
      // finer typical-day buckets on the short chart: 10 min over 3h ≈ 18 pts
      const bucket = range === "3h" ? 10 : 30;
      const [history, stats, profile] = await Promise.all([
        getJSON(`/api/sensors/history?metric=${m.id}&range=${range}`),
        getJSON(`/api/sensors/stats?metric=${m.id}`),
        getJSON(`/api/sensors/profile?metric=${m.id}&bucket=${bucket}`),
      ]);

      // "typical now": the 7-day average for the current time-of-day bucket
      const nowTod = secondsSinceMidnight();
      let typicalNow = null;
      if (profile.points.length) {
        let best = profile.points[0];
        for (const p of profile.points) {
          if (Math.abs(p.tod - nowTod) < Math.abs(best.tod - nowTod)) best = p;
        }
        typicalNow = best.value.toFixed(m.decimals);
      }
      setStat(widget, "min_24h", stats.min_24h, m.unit);
      setStat(widget, "max_24h", stats.max_24h, m.unit);
      setStat(widget, "avg_24h", stats.avg_24h, m.unit);
      setStat(widget, "typical_now", typicalNow, m.unit);

      // On the 3h and 24h views, overlay the typical-day curve mapped onto
      // the rolling window (each time-of-day occurs exactly once in 24h;
      // drawChart clips it to the visible span).
      let overlaySeries = null;
      if ((range === "3h" || range === "24h") && profile.points.length >= 2) {
        const now = Date.now() / 1000;
        overlaySeries = {
          label: "Typical day (7d avg)",
          points: profile.points
            .map((p) => ({ ts: now - ((nowTod - p.tod + 86400) % 86400), value: p.value }))
            .sort((a, b) => a.ts - b.ts),
        };
      }
      renderLegend(detail, m.color, overlaySeries, `Last ${range}`);
      drawChart(detail.querySelector(".chart"), history.points,
        { ...m, overlay: overlaySeries,
          thresholds: thresholds ? thresholds[m.id] : null });
    } else if (kind === "power") {
      const id = widget.dataset.deviceId;
      const [history, stats] = await Promise.all([
        getJSON(`/api/devices/${id}/power/history?range=${range}`),
        getJSON(`/api/devices/${id}/power/stats`),
      ]);
      const points = history.points
        .filter((p) => p.watts !== null)
        .map((p) => ({ ts: p.ts, value: p.watts }));
      renderLegend(detail, POWER_COLOR, null);
      drawChart(detail.querySelector(".chart"), points,
        { unit: "W", decimals: 1, color: POWER_COLOR,
          // watts integrate into energy; a dragged span on this chart should
          // answer "what did that cost me", not just "what was the average draw"
          energy: true,
          thresholds: thresholds ? thresholds.power : null });
      setStat(widget, "avg_24h_w", stats.avg_24h_w, "W");
      setStat(widget, "kwh_24h", stats.kwh_24h, "kWh");
      setStat(widget, "avg_7d_w", stats.avg_7d_w, "W");
    } else if (kind === "nights") {
      // Its own range/metric buttons drive it (see loadNights); the generic
      // range machinery does not apply. Always reopen on the list, never on
      // whichever night happened to be open last time.
      showNightsList();
      renderNights();
    } else if (kind === "motion") {
      const data = await getJSON(`/api/motion/events?range=${range}`);
      setStat(widget, "count", data.count, null);
      setStat(widget, "last", data.events.length ? relTime(data.events[0].ts) : "—", null);
      renderActivityLog(data.events);
    }
  } catch {
    /* link indicator already reports backend loss */
  }
}

function renderLegend(detail, color, overlaySeries, mainLabel) {
  let legend = detail.querySelector(".chart-legend");
  if (!overlaySeries) {
    if (legend) legend.remove();
    return;
  }
  if (!legend) {
    legend = document.createElement("div");
    legend.className = "chart-legend";
    detail.querySelector(".chart").before(legend);
  }
  legend.textContent = "";
  for (const [label, opacity] of [[mainLabel, 1], [overlaySeries.label, 0.45]]) {
    const item = document.createElement("span");
    const key = document.createElement("i");
    key.className = "line-key";
    key.style.borderTopColor = color;
    key.style.opacity = opacity;
    item.append(key, document.createTextNode(label));
    legend.appendChild(item);
  }
}

/* ------------------------------------------------------------ sparklines */

async function refreshSpark(widget) {
  const kind = widget.dataset.widget;
  try {
    let points;
    let color;
    if (kind === "metric") {
      const m = METRICS.find((x) => x.id === widget.dataset.metric);
      const data = await getJSON(`/api/sensors/history?metric=${m.id}&range=${SPARK_RANGE}`);
      points = data.points;
      color = m.color;
    } else if (kind === "power") {
      const data = await getJSON(
        `/api/devices/${widget.dataset.deviceId}/power/history?range=${SPARK_RANGE}`);
      points = data.points
        .filter((p) => p.watts !== null)
        .map((p) => ({ ts: p.ts, value: p.watts }));
      color = POWER_COLOR;
    } else {
      return;
    }
    drawSpark(widget.querySelector(".spark"), points, color);
  } catch { /* transient — next cycle retries */ }
}

function drawSpark(container, points, color) {
  container.textContent = "";
  if (!points || points.length < 2) return;
  const w = container.clientWidth || 200;
  const h = container.clientHeight || 36;
  const xs = points.map((p) => p.ts);
  const ys = points.map((p) => p.value);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const X = (ts) => ((ts - xMin) / (xMax - xMin)) * (w - 6) + 3;
  const Y = (v) => 3 + (h - 6) * (1 - (v - yMin) / (yMax - yMin));
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d",
    points.map((p, i) => `${i ? "L" : "M"}${X(p.ts).toFixed(1)},${Y(p.value).toFixed(1)}`).join(""));
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", color);
  path.setAttribute("stroke-width", 1.5);
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);
  container.appendChild(svg);
}

function refreshAllSparks() {
  document.querySelectorAll(".widget[data-widget='metric'], .widget[data-widget='power']")
    .forEach(refreshSpark);
}

/* ---------------------------------------------------------------- charts */

const tooltip = document.getElementById("chart-tooltip");

function drawChart(container, points, opts) {
  container.textContent = "";
  // A selection belongs to the chart that produced it — switching range or
  // metric must not leave last chart's statistics sitting underneath.
  const priorSelection = document.getElementById("chart-selection");
  if (priorSelection) priorSelection.hidden = true;
  if (!points || points.length < 2) {
    const empty = document.createElement("div");
    empty.className = "chart-empty";
    empty.textContent = "Not enough data in this range yet.";
    container.appendChild(empty);
    return;
  }

  const overlay = opts.overlay; // optional {label, points} second series
  const w = container.clientWidth || 300;
  const h = container.clientHeight || 240;
  const pad = { top: 8, right: 12, bottom: 20, left: 44 };
  const iw = w - pad.left - pad.right;
  const ih = h - pad.top - pad.bottom;

  const xs = points.map((p) => p.ts);
  const allYs = points.map((p) => p.value)
    .concat(overlay ? overlay.points.map((p) => p.value) : []);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = Math.min(...allYs), yMax = Math.max(...allYs);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const ySpanRaw = yMax - yMin;
  yMin -= ySpanRaw * 0.12; yMax += ySpanRaw * 0.12;
  // Headroom is fine above, but the 12% below can push the axis negative on a
  // quantity that has no negative values (see METRICS.allowNegative). Clamp
  // rather than skip the padding, so a series resting at 0 still sits off the
  // floor when it can go negative, and lands exactly on it when it can't.
  if (!opts.allowNegative) yMin = Math.max(0, yMin);
  const xSpan = xMax - xMin;

  const X = (ts) => pad.left + ((ts - xMin) / xSpan) * iw;
  const Y = (v) => pad.top + ih - ((v - yMin) / (yMax - yMin)) * ih;

  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);

  const el = (tag, attrs) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    svg.appendChild(node);
    return node;
  };

  const pathD = (pts) =>
    pts.map((p, i) => `${i ? "L" : "M"}${X(p.ts).toFixed(1)},${Y(p.value).toFixed(1)}`).join("");

  // gridlines + y ticks (3 levels; enough decimals that ticks stay distinct)
  const gridColor = cssVar("--grid");
  const mutedInk = cssVar("--muted");
  const span = yMax - yMin;
  const tickDecimals = span < 1 ? 2 : span < 8 ? 1 : 0;
  for (const frac of [0, 0.5, 1]) {
    const v = yMin + span * frac;
    const y = Y(v);
    el("line", { x1: pad.left, y1: y, x2: w - pad.right, y2: y,
                 stroke: gridColor, "stroke-width": 1 });
    const label = el("text", { x: pad.left - 6, y: y + 3.5, "text-anchor": "end",
                               fill: mutedInk, "font-size": 10,
                               "font-family": "ui-monospace, Menlo, monospace" });
    label.textContent = tickLabel(v, tickDecimals);
  }

  // x labels: first and last timestamps
  for (const [ts, anchor] of [[xMin, "start"], [xMax, "end"]]) {
    const label = el("text", { x: X(ts), y: h - 5, "text-anchor": anchor,
                               fill: mutedInk, "font-size": 10,
                               "font-family": "ui-monospace, Menlo, monospace" });
    label.textContent = axisTime(ts, xSpan);
  }

  // typical-day overlay: same hue, de-emphasized, under the main line
  const overlayPts = overlay
    ? overlay.points.filter((p) => p.ts >= xMin && p.ts <= xMax)
    : [];
  if (overlayPts.length >= 2) {
    el("path", { d: pathD(overlayPts), fill: "none", stroke: opts.color,
                 opacity: 0.4, "stroke-width": 2,
                 "stroke-linejoin": "round", "stroke-linecap": "round" });
  }

  // area wash (~10% opacity) + 2px line + end dot with surface ring
  const lineD = pathD(points);
  const areaBelowD = `${lineD}L${X(xMax).toFixed(1)},${Y(yMin).toFixed(1)}L${X(xMin).toFixed(1)},${Y(yMin).toFixed(1)}Z`;
  el("path", { d: areaBelowD, fill: opts.color, opacity: 0.1 });

  // thresholds: dotted line at the bound; the exceedance area between the
  // curve and that line fills red (the curve-hugging shape comes from
  // clipping the full above/below-curve polygon to the threshold side)
  if (opts.thresholds) {
    const critical = cssVar("--critical");
    const { min: tMin, max: tMax } = opts.thresholds;
    const areaAboveD = `${lineD}L${X(xMax).toFixed(1)},${pad.top}L${X(xMin).toFixed(1)},${pad.top}Z`;
    const defs = document.createElementNS(NS, "defs");
    svg.appendChild(defs);
    const uid = Math.random().toString(36).slice(2, 8);

    const clipRect = (id, y1, y2) => {
      const clip = document.createElementNS(NS, "clipPath");
      clip.setAttribute("id", id);
      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", pad.left);
      rect.setAttribute("y", y1);
      rect.setAttribute("width", iw);
      rect.setAttribute("height", Math.max(y2 - y1, 0));
      clip.appendChild(rect);
      defs.appendChild(clip);
    };

    const drawBound = (value, isMax) => {
      if (value === null || value === undefined) return;
      const visible = isMax ? value < yMax : value > yMin; // any exceedance side on screen
      if (!visible) return;
      const edge = Y(Math.min(Math.max(value, yMin), yMax));
      const clipId = `th-${uid}-${isMax ? "max" : "min"}`;
      if (isMax) clipRect(clipId, pad.top, edge);
      else clipRect(clipId, edge, pad.top + ih);
      el("path", { d: isMax ? areaBelowD : areaAboveD, fill: critical,
                   opacity: 0.2, "clip-path": `url(#${clipId})` });
      if (value >= yMin && value <= yMax) { // line only when the bound is on screen
        el("line", { x1: pad.left, y1: edge, x2: w - pad.right, y2: edge,
                     stroke: critical, "stroke-width": 1.5, opacity: 0.75,
                     "stroke-linecap": "round", "stroke-dasharray": "1 5" });
        const label = el("text", { x: w - pad.right - 4,
                                   y: isMax ? edge - 5 : edge + 12,
                                   "text-anchor": "end", fill: critical,
                                   "font-size": 9, opacity: 0.85,
                                   "font-family": "ui-monospace, Menlo, monospace" });
        label.textContent = `${isMax ? "max" : "min"} ${value}`;
      }
    };
    drawBound(tMax, true);
    drawBound(tMin, false);
  }

  el("path", { d: lineD, fill: "none", stroke: opts.color, "stroke-width": 2,
               "stroke-linejoin": "round", "stroke-linecap": "round" });
  const last = points[points.length - 1];
  el("circle", { cx: X(last.ts), cy: Y(last.value), r: 4, fill: opts.color,
                 stroke: cssVar("--surface"), "stroke-width": 2 });

  // crosshair + tooltip (lists both series at the hovered X)
  const cross = el("line", { x1: 0, y1: pad.top, x2: 0, y2: pad.top + ih,
                             stroke: cssVar("--baseline"), "stroke-width": 1, opacity: 0 });
  const hoverDot = el("circle", { r: 4.5, fill: opts.color,
                                  stroke: cssVar("--surface"), "stroke-width": 2, opacity: 0 });

  const nearestOf = (pts, ts) => {
    let best = pts[0];
    for (const p of pts) if (Math.abs(p.ts - ts) < Math.abs(best.ts - ts)) best = p;
    return best;
  };

  /* Drag across the chart to select a span and get its statistics. The chart
     already answers "what was it at this moment" via the crosshair; this
     answers "what was it across this stretch", which otherwise means eyeballing
     a line. Selection is horizontal only — the question is always about a time
     range, never a value range. */
  const selRect = el("rect", { x: 0, y: pad.top, width: 0, height: ih,
                               fill: opts.color, opacity: 0.14 });
  const selEdges = [0, 1].map(() =>
    el("line", { x1: 0, y1: pad.top, x2: 0, y2: pad.top + ih,
                 stroke: opts.color, "stroke-width": 1, opacity: 0 }));
  const readout = document.getElementById("chart-selection");

  let dragFrom = null;   // timestamp where the drag started
  let dragging = false;

  const tsAt = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const raw = xMin + ((clientX - rect.left) / rect.width * w - pad.left) / iw * xSpan;
    return Math.min(Math.max(raw, xMin), xMax);   // clamp to the plotted range
  };

  function clearSelection() {
    selRect.setAttribute("width", 0);
    selEdges.forEach((l) => l.setAttribute("opacity", 0));
    if (readout) readout.hidden = true;
  }

  function paintSelection(a, b) {
    const [lo, hi] = a <= b ? [a, b] : [b, a];
    const x1 = X(lo), x2 = X(hi);
    selRect.setAttribute("x", x1);
    selRect.setAttribute("width", Math.max(x2 - x1, 0));
    selEdges[0].setAttribute("x1", x1); selEdges[0].setAttribute("x2", x1);
    selEdges[1].setAttribute("x1", x2); selEdges[1].setAttribute("x2", x2);
    selEdges.forEach((l) => l.setAttribute("opacity", 0.9));

    const inside = points.filter((p) => p.ts >= lo && p.ts <= hi);
    if (!readout) return;
    // Below two samples an "average" is just the sample, which is misleading
    // dressed up as a statistic.
    if (inside.length < 2) {
      readout.hidden = false;
      readout.textContent = "Drag across at least two points";
      return;
    }
    const vals = inside.map((p) => p.value);
    const avg = vals.reduce((s, v) => s + v, 0) / vals.length;
    const mins = Math.round((hi - lo) / 60);
    const dur = mins >= 120 ? `${(mins / 60).toFixed(1)} h` : `${mins} min`;
    readout.hidden = false;
    readout.textContent = "";
    const strong = document.createElement("span");
    strong.className = "tt-value";
    strong.textContent = `avg ${avg.toFixed(opts.decimals)} ${opts.unit}`;
    readout.append(strong);

    // Watts are a rate, so a span of them integrates into energy — the number
    // you actually want off a power chart ("what did that run cost me"), which
    // an average draw alone does not give you.
    //
    // Trapezoidal over the selected points, NOT avg x duration: the series is
    // bucket-averaged server-side and can have gaps where the plug was
    // unreachable, and avg x duration silently bills you for those gaps at the
    // average rate. Trapezoid only ever integrates between samples that exist.
    if (opts.energy) {
      let wh = 0;
      for (let i = 1; i < inside.length; i++) {
        const dtHours = (inside[i].ts - inside[i - 1].ts) / 3600;
        wh += ((inside[i].value + inside[i - 1].value) / 2) * dtHours;
      }
      const kwh = wh / 1000;
      const energy = document.createElement("span");
      energy.className = "tt-value";
      // Below ~10 Wh, kWh renders as 0.00 and looks broken; show Wh instead.
      energy.textContent = wh < 10 ? `${wh.toFixed(1)} Wh` : `${kwh.toFixed(3)} kWh`;
      readout.append(energy);
    }

    readout.append(
      document.createTextNode(
        `min ${Math.min(...vals).toFixed(opts.decimals)} · ` +
        `max ${Math.max(...vals).toFixed(opts.decimals)} · ` +
        `${axisTime(lo, xSpan)}–${axisTime(hi, xSpan)} · ${dur} · ${inside.length} pts`),
    );
  }

  svg.addEventListener("pointerdown", (ev) => {
    dragFrom = tsAt(ev.clientX);
    dragging = true;
    svg.setPointerCapture(ev.pointerId);
    clearSelection();
  });

  svg.addEventListener("pointerup", (ev) => {
    if (!dragging) return;
    dragging = false;
    try { svg.releasePointerCapture(ev.pointerId); } catch { /* already gone */ }
    // A click with no drag clears rather than selecting a zero-width span.
    if (Math.abs(tsAt(ev.clientX) - dragFrom) < xSpan * 0.005) clearSelection();
    dragFrom = null;
  });

  /* Touch only: the browser claims the gesture as a page scroll and sends
     pointercancel instead of pointerup. Without this the drag stayed "open",
     and the next tap on the chart painted a selection running from wherever
     the abandoned one started. (touch-action: pan-y in styles.css is the
     other half — it gives vertical swipes to the page and horizontal ones to
     the selection, so drag-to-select still works with a finger.) */
  svg.addEventListener("pointercancel", (ev) => {
    dragging = false;
    dragFrom = null;
    try { svg.releasePointerCapture(ev.pointerId); } catch { /* already gone */ }
  });

  svg.addEventListener("pointermove", (ev) => {
    if (dragging && dragFrom !== null) paintSelection(dragFrom, tsAt(ev.clientX));
    const rect = svg.getBoundingClientRect();
    const ts = xMin + ((ev.clientX - rect.left) / rect.width * w - pad.left) / iw * xSpan;
    const nearest = nearestOf(points, ts);
    const px = X(nearest.ts);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px);
    cross.setAttribute("opacity", 1);
    hoverDot.setAttribute("cx", px); hoverDot.setAttribute("cy", Y(nearest.value));
    hoverDot.setAttribute("opacity", 1);

    tooltip.hidden = false;
    tooltip.textContent = "";
    const strong = document.createElement("span");
    strong.className = "tt-value";
    strong.textContent = `${Number(nearest.value).toFixed(opts.decimals)} ${opts.unit}`;
    tooltip.appendChild(strong);
    if (overlayPts.length >= 2) {
      const typ = nearestOf(overlayPts, nearest.ts);
      tooltip.appendChild(document.createTextNode(
        `typical ${Number(typ.value).toFixed(opts.decimals)} ${opts.unit}`));
      tooltip.appendChild(document.createElement("br"));
    }
    tooltip.appendChild(document.createTextNode(axisTime(nearest.ts, xSpan)));
    const tx = Math.min(ev.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8);
    tooltip.style.left = `${tx}px`;
    tooltip.style.top = `${ev.clientY - 10 - tooltip.offsetHeight}px`;
  });
  svg.addEventListener("pointerleave", () => {
    cross.setAttribute("opacity", 0);
    hoverDot.setAttribute("opacity", 0);
    tooltip.hidden = true;
  });

  container.appendChild(svg);
}

function tickLabel(v, decimals) {
  if (Math.abs(v) >= 10000) return `${(v / 1000).toFixed(1)}k`;
  return Number(v).toFixed(decimals);
}

/* ---------------------------------------------------------- activity log */

function renderActivityLog(events) {
  const log = document.getElementById("activity-log");
  log.textContent = "";
  // While Home, every entry would be you crossing your own room — the log
  // fills up and stops carrying information. Say so rather than showing a
  // misleading empty list.
  if (motionSuppressed()) {
    const li = document.createElement("li");
    li.className = "log-empty";
    li.textContent = "Paused while Home — switch to Away or Sleeping to log motion.";
    log.appendChild(li);
    return;
  }
  if (!events.length) {
    const li = document.createElement("li");
    li.className = "log-empty";
    li.textContent = "No motion in this range yet.";
    log.appendChild(li);
    return;
  }
  for (const ev of events) {
    const li = document.createElement("li");
    const what = document.createElement("span");
    what.textContent = "Motion detected";
    const when = document.createElement("span");
    when.className = "log-time";
    when.textContent = `${axisTime(ev.ts, 0)} · ${relTime(ev.ts)}`;
    li.append(what, when);
    log.appendChild(li);
  }
}

/* ------------------------------------------------------------- nights
   Every recorded Sleeping window: a trend across 7/30/60 days plus a
   per-night list. Anomalies come from the server, which flags a metric more
   than 2 SD from the window's own mean — relative rather than absolute,
   because "cold" only means anything against your own nights and a fixed
   threshold would light up every night in winter. */

const NIGHT_METRICS = {
  temp: { label: "Temp", unit: "°C", decimals: 1, pick: (n) => n.temp && n.temp.avg },
  hum: { label: "Humidity", unit: "%RH", decimals: 1, pick: (n) => n.hum && n.hum.avg },
  co2: { label: "CO₂", unit: "ppm", decimals: 0, pick: (n) => n.co2 && n.co2.avg },
  motion: { label: "Got up", unit: "×", decimals: 0, pick: (n) => n.motion && n.motion.count },
};

let nightsData = [];
let nightsRange = "7d";
let nightsMetric = "temp";

async function loadNights() {
  try {
    const data = await getJSON(`/api/nights?range=${nightsRange}`);
    nightsData = data.nights || [];
  } catch {
    return;
  }
  document.getElementById("nights-count").textContent = nightsData.length || "—";
  document.getElementById("nights-note").textContent = nightsData.length
    ? `Last ${nightsRange}. Click a night to inspect it.`
    : "No Sleeping windows recorded yet.";
  renderNights();
}

function renderNights() {
  const spec = NIGHT_METRICS[nightsMetric];
  // oldest-first for the chart; the API returns newest-first for the list
  const points = nightsData.slice().reverse()
    .map((n) => ({ ts: n.from, value: spec.pick(n) }))
    .filter((p) => p.value !== null && p.value !== undefined);

  const chart = document.getElementById("nights-chart");
  if (chart) {
    drawChart(chart, points, {
      color: cssVar("--s-temp"), unit: spec.unit, decimals: spec.decimals,
      allowNegative: nightsMetric === "temp",
    });
  }

  const values = points.map((p) => p.value);
  const stats = document.getElementById("nights-stats");
  stats.textContent = "";
  if (values.length) {
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    stats.append(
      summaryStat(`${spec.label} avg`, mean.toFixed(spec.decimals), spec.unit, null, false),
      summaryStat("Best", Math.min(...values).toFixed(spec.decimals), spec.unit, null, false),
      summaryStat("Worst", Math.max(...values).toFixed(spec.decimals), spec.unit, null, false),
      summaryStat("Nights", values.length, null, null, false),
    );
  }

  const list = document.getElementById("nights-list");
  list.textContent = "";
  if (!nightsData.length) {
    const li = document.createElement("li");
    li.className = "log-empty";
    li.textContent = "No nights recorded yet.";
    list.appendChild(li);
    return;
  }
  for (const n of nightsData) {
    const li = document.createElement("li");
    li.className = "night-row";
    li.tabIndex = 0;

    const date = document.createElement("span");
    date.className = "night-date";
    date.textContent = n.night;

    const vals = document.createElement("span");
    vals.className = "night-vals";
    const cell = (metric, text) => {
      const el = document.createElement("span");
      el.textContent = text;
      // The server decides what is anomalous; the frontend only colours it.
      if ((n.anomalies || []).includes(metric)) el.className = "night-anom";
      return el;
    };
    const hours = ((n.to - n.from) / 3600).toFixed(1);
    vals.append(
      cell(null, `${hours} h`),
      cell("temp", n.temp && n.temp.avg !== null ? `${n.temp.avg} °C` : "—"),
      cell("hum", n.hum && n.hum.avg !== null ? `${n.hum.avg} %` : "—"),
      cell("co2", n.co2 && n.co2.avg !== null && n.co2.avg !== undefined ? `${n.co2.avg} ppm` : "—"),
      cell("motion", `${(n.motion && n.motion.count) || 0}× up`),
    );

    li.append(date, vals);
    const open = () => openNightDetail(n.night);
    li.addEventListener("click", open);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
    list.appendChild(li);
  }
}

/* Drill down inside the widget overlay rather than opening a second modal on
   top of it — the old dialog rendered underneath and had to be closed before
   the night could be read. */

const nightsListPane = document.getElementById("nights-list-pane");
const nightsDetailPane = document.getElementById("nights-detail-pane");
let nightDetailNight = null;

function showNightsList() {
  nightsDetailPane.hidden = true;
  nightsListPane.hidden = false;
  resetNightDelete();
  nightDetailNight = null;
}

async function openNightDetail(night) {
  let data;
  try {
    data = await getJSON(`/api/nights/${encodeURIComponent(night)}`);
  } catch {
    return;
  }
  nightDetailNight = data.night;
  resetNightDelete();

  document.getElementById("night-detail-title").textContent = `Night of ${data.night}`;
  const hours = ((data.to - data.from) / 3600).toFixed(1);
  document.getElementById("night-detail-window").textContent =
    `Sleeping ${axisTime(data.from, 0)} → ${axisTime(data.to, 0)} · ${hours} h`;

  const stats = document.getElementById("night-detail-stats");
  stats.textContent = "";
  const range = (st) => (st && st.min !== null && st.min !== undefined
    ? `min ${st.min} · max ${st.max}` : null);
  stats.append(
    summaryStat("Temp avg", data.temp && data.temp.avg, "°C", range(data.temp), false),
    summaryStat("Humidity avg", data.hum && data.hum.avg, "%RH", range(data.hum), false),
  );
  if (data.co2 && data.co2.avg !== null && data.co2.avg !== undefined) {
    stats.append(summaryStat("CO₂ avg", data.co2.avg, "ppm",
      data.co2.start === null || data.co2.start === undefined
        ? null : `${data.co2.start} → ${data.co2.end} ppm`,
      data.co2.rose_significantly));
  }
  const got = (data.motion && data.motion.count) || 0;
  stats.append(summaryStat("Got up", got, got === 1 ? "time" : "times",
    got === 0 ? "slept through" : null, false));

  const extra = document.getElementById("night-detail-extra");
  extra.textContent = "";
  const times = (data.motion && data.motion.events) || [];
  if (times.length) {
    const p = document.createElement("p");
    p.className = "card-note";
    p.textContent = "Up at " + times.map((t) => axisTime(t, 0)).join(", ");
    extra.appendChild(p);
  }

  nightsListPane.hidden = true;
  nightsDetailPane.hidden = false;
}

/* Delete, behind an inline confirm rather than a browser confirm() so it
   matches the rest of the board. Two-step because it is irreversible. */
function resetNightDelete() {
  document.getElementById("night-delete").hidden = false;
  document.getElementById("night-delete-confirm").hidden = true;
}

document.getElementById("nights-back").addEventListener("click", showNightsList);

document.getElementById("night-delete").addEventListener("click", () => {
  document.getElementById("night-delete").hidden = true;
  document.getElementById("night-delete-confirm").hidden = false;
});
document.getElementById("night-delete-no").addEventListener("click", resetNightDelete);
document.getElementById("night-delete-yes").addEventListener("click", async () => {
  if (!nightDetailNight) return;
  try {
    await fetch(`/api/nights/${encodeURIComponent(nightDetailNight)}`, { method: "DELETE" });
  } catch {
    return;
  }
  showNightsList();
  loadNights();   // averages and anomaly flags shift once a junk night is gone
});

document.querySelectorAll("[data-nrange]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-nrange]").forEach((b) => b.classList.toggle("active", b === btn));
    nightsRange = btn.dataset.nrange;
    loadNights();
  });
});
document.querySelectorAll("[data-nmetric]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-nmetric]").forEach((b) => b.classList.toggle("active", b === btn));
    nightsMetric = btn.dataset.nmetric;
    renderNights();
  });
});

/* ------------------------------------------------------------ view switch
   BOARD is the device dashboard; PLANNER (calendar + to-do) and HEALTH
   (sleep/recovery) are the non-device views. Device types always get a new
   .zone on the board — a view of its own is only for non-device surfaces. */

const boardView = document.getElementById("view-board");
const plannerView = document.getElementById("view-planner");
const healthView = document.getElementById("view-health");
const views = { board: boardView, planner: plannerView, health: healthView };
const viewButtons = [...document.querySelectorAll(".view-btn")];

function showView(name) {
  if (!(name in views)) name = "board";
  for (const [key, el] of Object.entries(views)) el.hidden = key !== name;
  for (const btn of viewButtons) {
    const on = btn.dataset.view === name;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", String(on));
  }
  if (name === "planner") refreshPlanner();
  if (name === "health") refreshHealth();
  // deep-linkable, like the widget hashes
  history.replaceState(null, "",
    name === "board" ? location.pathname + location.search : `#${name}`);
}

viewButtons.forEach((btn) => btn.addEventListener("click", () => showView(btn.dataset.view)));

/* ----------------------------------------------------------------- planner
   Calendar (day/week/month time grid) over /api/events + a slim to-do list
   over /api/tasks. Events are created through the + button or by dragging:
   a drag on the day/week grid (which can cross day columns → a multi-day
   timed event) or across month cells (→ an all-day event, since month is
   day-granular). All open the event dialog (#event-overlay), which doubles
   as the detail/edit view when an existing event/bar/chip is clicked.
   All-day events render as spanning bars in the all-day row (day/week) and
   as continuation chips across month cells; timed multi-day events clip into
   each day column. Tasks add/edit through a small inline row behind the +. */

/* ONE persisted calendar knob, --cal-size: the physical size of the calendar
   WINDOW on the page (the visible height of the grid box, the month row
   height, and the page width the planner claims). Set here, every dimension
   derived from it in styles.css.

   There was a second stepper for --cal-hour-px, the height of one hour INSIDE
   that window. It is now FIXED at 28px in styles.css (:root) — the user
   settled on that value, which the stepper displayed as "50%", and asked for
   the control to go. The property stays the single definition both files
   share: styles.css declares it, calHourPx() below reads the computed value
   back rather than hardcoding 28 a second time. That is the whole point of
   the arrangement — the hour height used to be duplicated in JS and in three
   CSS rules behind a "keep in sync" comment, which is not a mechanism.

   The window height used to come from a CSS `resize: vertical` grip on
   .cal-scroll, captured with a ResizeObserver — and that fed back on itself.
   box-sizing is border-box and .cal-scroll carries a 1px top border, so
   contentRect.height reported one pixel LESS than the max-height that produced
   it. Saving that back shrank the box, which the observer saw, and saved
   shorter again: the calendar walked itself smaller a pixel at a time, every
   render, and localStorage kept the result across reloads. An explicit stepper
   has no such loop — nothing reads a size back out of the layout. */
const CAL_SIZE_KEY = "hub-cal-size";
const CAL_SIZE_MIN = 0.7;
const CAL_SIZE_MAX = 1.6;
const CAL_SIZE_STEP = 0.1;
const CAL_HOUR_FALLBACK = 28;  // only if the CSS custom property is missing

// Keys from controls that no longer exist. Dropped rather than migrated: the
// viewport-px value was eaten by the shrink loop described above, and the
// hour-px value is now fixed in CSS, so a stored one would be ignored anyway
// and only confuse anyone reading localStorage later.
localStorage.removeItem("hub-cal-viewport-px");
localStorage.removeItem("hub-cal-hour-px");

function calHourPx() {
  const v = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue("--cal-hour-px"));
  return Number.isFinite(v) && v > 0 ? v : CAL_HOUR_FALLBACK;
}

function calSize() {
  const v = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue("--cal-size"));
  return Number.isFinite(v) && v > 0 ? v : 1;
}

/* No redraw: nothing inside the grid is positioned from --cal-size, CSS
   derives the whole box. That also means the scroll position survives. */
function setCalSize(size, { persist = true } = {}) {
  const clamped = Math.round(
    Math.min(Math.max(size, CAL_SIZE_MIN), CAL_SIZE_MAX) * 100) / 100;
  document.documentElement.style.setProperty("--cal-size", String(clamped));
  if (persist) localStorage.setItem(CAL_SIZE_KEY, String(clamped));
  const label = document.getElementById("cal-size-label");
  if (label) label.textContent = `${Math.round(clamped * 100)}%`;
}

/* On a phone the size is forced to 1 and its stepper is hidden (there is one
   sane calendar size on a 390px screen, and it is "the screen"). Nothing is
   persisted in that state: the same localStorage is shared with the desktop
   layout when a window is merely dragged narrow, and a visit at phone width
   must not overwrite the size chosen there. The hour scale needs no handling
   either way — it is a constant in styles.css now. */
function restoreCalendarSizing() {
  const phone = isPhone();
  const savedSize = parseFloat(localStorage.getItem(CAL_SIZE_KEY));
  setCalSize(!phone && Number.isFinite(savedSize) ? savedSize : 1, { persist: !phone });
}
restoreCalendarSizing();

/* Crossing the breakpoint (rotation, or a resized desktop window) re-applies
   the sizing and re-picks the calendar view, so the layout never lands in a
   state neither side would have produced. */
PHONE_QUERY.addEventListener("change", () => {
  restoreCalendarSizing();
  if (calView !== "month") calView = isPhone() ? "day" : "week";
  loadEvents();  // the view change moves the fetch window, so refetch, not redraw
});

/* Wired at load (this script runs at the end of body). */
function wireCalendarSizing() {
  const resize = (delta) => setCalSize(calSize() + delta);
  document.getElementById("cal-size-in").addEventListener("click", () => resize(CAL_SIZE_STEP));
  document.getElementById("cal-size-out").addEventListener("click", () => resize(-CAL_SIZE_STEP));
  document.getElementById("cal-size-reset").addEventListener("click", () => setCalSize(1));
}
wireCalendarSizing();
const CAL_SNAP_MIN = 15;  // drag-to-create snaps to quarter hours
const CATEGORIES = ["home", "work", "personal", "health", "social"]; // keep in sync with app/planner.py

/* Week is the desktop default; a phone gets Day, because seven columns in
   ~330px truncates every title to one character. renderCalendar() syncs the
   toolbar buttons, so the markup's hardcoded `active` needs no counterpart
   here. */
let calView = isPhone() ? "day" : "week";   // "day" | "week" | "month"
let calAnchor = new Date();  // any date inside the shown period
let calScrollTop = null;     // preserved across grid rebuilds
let calDragging = false;     // blocks the periodic refresh mid-drag
let plannerEvents = [];      // expanded occurrences for the current window
let plannerTasks = [];       // every task; each widget filters this to its own list
let dialogEvent = null;      // occurrence being edited in the dialog, or null

const calendarEl = document.getElementById("calendar");
const calTitleEl = document.getElementById("cal-title");
const eventOverlay = document.getElementById("event-overlay");
const eventForm = document.getElementById("event-form");
const eventTitle = document.getElementById("event-title");
const eventStart = document.getElementById("event-start");
const eventEnd = document.getElementById("event-end");
const eventRecurrence = document.getElementById("event-recurrence");
const eventNotes = document.getElementById("event-notes");
const eventNote = document.getElementById("event-note");
const eventAllday = document.getElementById("event-allday");

const pad2 = (n) => String(n).padStart(2, "0");
const dateKey = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;

// epoch seconds -> "YYYY-MM-DDTHH:MM" for a datetime-local input
function toLocalInput(ts) {
  const d = new Date(ts * 1000);
  return `${dateKey(d)}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// compact 24h "HH:MM", same style as the header's wake-time note
function fmtHM(ts) {
  const d = new Date(ts * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function mondayOf(d) { const x = startOfDay(d); x.setDate(x.getDate() - (x.getDay() + 6) % 7); return x; }

const isAllDay = (ev) => ev.all_day;

// display end (epoch s): all-day single day (end null) covers its whole day;
// timed no-end events get a 1h block. Multi-day events carry an explicit end.
function effectiveEnd(ev) {
  if (ev.end !== null) return ev.end;
  return ev.start + (ev.all_day ? 86400 : 3600);
}

// whole days an all-day event covers (end is the exclusive day-after boundary)
function allDaySpanDays(ev) {
  return ev.end === null ? 1 : Math.round((ev.end - ev.start) / 86400);
}

const shortDate = (ts) =>
  new Date(ts * 1000).toLocaleDateString([], { day: "numeric", month: "short" });

// true when an event covers more than one calendar day (timed or all-day)
function spansDays(ev) {
  return dateKey(new Date(ev.start * 1000)) !== dateKey(new Date((effectiveEnd(ev) - 1) * 1000));
}

function eventTimeLabel(ev) {
  if (ev.all_day) {
    if (allDaySpanDays(ev) <= 1) return "all day";
    // inclusive last day = one second before the exclusive end
    return `all day · ${shortDate(ev.start)} – ${shortDate(ev.end - 1)}`;
  }
  if (ev.end === null) return fmtHM(ev.start);
  return spansDays(ev)
    ? `${shortDate(ev.start)} ${fmtHM(ev.start)} – ${shortDate(ev.end)} ${fmtHM(ev.end)}`
    : `${fmtHM(ev.start)}–${fmtHM(ev.end)}`;
}

async function refreshPlanner() {
  await Promise.all([loadEvents(), loadTasks()]);
}

async function loadEvents() {
  const { from, days } = calWindow();
  try {
    plannerEvents = (await getJSON(`/api/events?from=${dateKey(from)}&range=${days}d`)).events;
  } catch { return; } // link indicator already reports backend loss
  renderCalendar();
}

async function loadTasks() {
  try {
    plannerTasks = (await getJSON("/api/tasks")).tasks;
  } catch { return; }
  renderTasks();
}

/* ---- calendar window / navigation ---- */

function calWindow() {
  if (calView === "day") return { from: startOfDay(calAnchor), days: 1 };
  if (calView === "week") return { from: mondayOf(calAnchor), days: 7 };
  // month: whole weeks from the Monday before (or on) the 1st — 4 to 6 rows
  const first = new Date(calAnchor.getFullYear(), calAnchor.getMonth(), 1);
  const from = mondayOf(first);
  const daysInMonth = new Date(calAnchor.getFullYear(), calAnchor.getMonth() + 1, 0).getDate();
  const offset = Math.round((first - from) / 86400e3);
  return { from, days: Math.ceil((offset + daysInMonth) / 7) * 7 };
}

function calTitle() {
  if (calView === "day") {
    /* "Tue, 4 Aug" on a phone. The long form is ~190px of mono, which is
       exactly enough to push the + button onto a third toolbar row — and the
       year is not information you need while paging through days. */
    return isPhone()
      ? calAnchor.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })
      : calAnchor.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  }
  if (calView === "month") {
    return calAnchor.toLocaleDateString([], { month: "long", year: "numeric" });
  }
  const from = mondayOf(calAnchor), to = addDays(from, 6);
  const f = from.toLocaleDateString([], from.getMonth() === to.getMonth()
    ? { day: "numeric" } : { day: "numeric", month: "short" });
  return `${f} – ${to.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })}`;
}

function calStep(dir) {
  if (calView === "day") calAnchor = addDays(calAnchor, dir);
  else if (calView === "week") calAnchor = addDays(calAnchor, 7 * dir);
  else calAnchor = new Date(calAnchor.getFullYear(), calAnchor.getMonth() + dir, 1);
  loadEvents();
}

function switchToDay(day) {
  calView = "day";
  calAnchor = day;
  loadEvents();
}

document.querySelectorAll("#cal-view .range-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.calview === calView) return;
    calView = btn.dataset.calview;
    loadEvents();
  });
});
document.getElementById("cal-prev").addEventListener("click", () => calStep(-1));
document.getElementById("cal-next").addEventListener("click", () => calStep(1));
document.getElementById("cal-today").addEventListener("click", () => {
  calAnchor = new Date();
  loadEvents();
});

/* ---- calendar rendering ---- */

function renderCalendar() {
  document.querySelectorAll("#cal-view .range-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.calview === calView));
  calTitleEl.textContent = calTitle();
  calendarEl.textContent = "";
  if (calView === "month") renderMonth();
  else renderTimeGrid(calView === "day" ? 1 : 7);
}

function eventsOverlapping(startTs, endTs) {
  return plannerEvents.filter((e) => e.start < endTs && effectiveEnd(e) > startTs);
}

// greedy lane assignment inside each cluster of overlapping events, so
// simultaneous events share the column width side by side
function layoutLanes(events, dayStart, dayEnd) {
  const items = events.map((ev) => ({
    ev,
    top: Math.max(ev.start, dayStart),
    bottom: Math.min(effectiveEnd(ev), dayEnd),
  })).sort((a, b) => a.top - b.top || b.bottom - a.bottom);
  const out = [];
  let cluster = [], laneEnds = [], clusterEnd = -Infinity;
  const flush = () => {
    for (const item of cluster) { item.laneCount = laneEnds.length; out.push(item); }
    cluster = []; laneEnds = [];
  };
  for (const item of items) {
    if (cluster.length && item.top >= clusterEnd) flush();
    let lane = laneEnds.findIndex((end) => end <= item.top);
    if (lane === -1) { lane = laneEnds.length; laneEnds.push(0); }
    laneEnds[lane] = item.bottom;
    item.lane = lane;
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, item.bottom);
  }
  flush();
  return out;
}

// one positioned block on the time grid; API data goes in via textContent only
function calBlock(item, dayStart) {
  const { ev, lane, laneCount, top, bottom } = item;
  const el = document.createElement("button");
  el.type = "button";
  el.className = "cal-event" + (ev.category ? ` cat-${ev.category}` : "")
    + (ev.source === "caldav" ? " cal-imported" : "");
  el.style.top = `${((top - dayStart) / 3600) * calHourPx() + 1}px`;
  el.style.height = `${Math.max(((bottom - top) / 3600) * calHourPx() - 2, 19)}px`;
  el.style.left = `${(lane / laneCount) * 100}%`;
  el.style.width = `calc(${(100 / laneCount).toFixed(3)}% - 3px)`;
  const title = document.createElement("span");
  title.className = "cal-event-title";
  title.textContent = (ev.recurrence !== "none" ? "↻ " : "") + ev.title;
  el.appendChild(title);
  // a multi-day block is clipped per day, so a start–end time line would
  // misread as a same-day span; show it only on single-day events
  if (!spansDays(ev) && bottom - top >= 2700) { // room from ~45 min up
    const time = document.createElement("span");
    time.className = "cal-event-time";
    time.textContent = eventTimeLabel(ev);
    el.appendChild(time);
  }
  el.title = `${ev.title} · ${eventTimeLabel(ev)}`;
  el.addEventListener("click", () => openEventDialog(ev));
  return el;
}

// compact chip for the all-day row and month cells. clipStart/clipEnd add
// ‹ / › arrows when a multi-day bar continues beyond the visible edge.
function calChip(ev, withTime, clipStart, clipEnd) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "cal-chip" + (ev.category ? ` cat-${ev.category}` : "")
    + (ev.source === "caldav" ? " cal-imported" : "");
  const dot = document.createElement("i");
  dot.className = "cal-dot";
  chip.appendChild(dot);
  if (withTime && !isAllDay(ev)) {
    const t = document.createElement("span");
    t.className = "cal-chip-time";
    t.textContent = fmtHM(ev.start);
    chip.appendChild(t);
  }
  const label = (clipStart ? "‹ " : "") + ev.title + (clipEnd ? " ›" : "");
  chip.appendChild(document.createTextNode(label));
  chip.title = `${ev.title} · ${eventTimeLabel(ev)}`;
  chip.addEventListener("click", (e) => {
    e.stopPropagation(); // a month cell click underneath would open "new event"
    openEventDialog(ev);
  });
  return chip;
}

function renderTimeGrid(nDays) {
  const { from } = calWindow();
  const days = Array.from({ length: nDays }, (_, i) => addDays(from, i));
  const todayKey = dateKey(new Date());
  const cols = `56px repeat(${nDays}, minmax(0, 1fr))`;

  // day headers (the toolbar title already names a single day)
  if (nDays > 1) {
    const head = document.createElement("div");
    head.className = "cal-head-row";
    head.style.gridTemplateColumns = cols;
    head.appendChild(document.createElement("div")); // gutter spacer
    for (const day of days) {
      const h = document.createElement("button");
      h.type = "button";
      h.className = "cal-day-head" + (dateKey(day) === todayKey ? " today" : "");
      h.title = "Open day view";
      h.appendChild(document.createTextNode(day.toLocaleDateString([], { weekday: "short" })));
      const num = document.createElement("span");
      num.className = "cal-day-num";
      num.textContent = day.getDate();
      h.appendChild(num);
      h.addEventListener("click", () => switchToDay(day));
      head.appendChild(h);
    }
    calendarEl.appendChild(head);
  }

  // all-day row: each all-day event is one bar spanning the day columns it
  // covers (clamped to the visible window), lane-packed so events on disjoint
  // days share a line. Multi-day *timed* events aren't here — they clip into
  // the grid columns below instead.
  const dayIndexOf = (ts) => Math.round((startOfDay(new Date(ts * 1000)) - from) / 86400e3);
  const bars = plannerEvents
    .filter(isAllDay)
    .map((ev) => {
      const rawStart = dayIndexOf(ev.start);
      const rawEnd = dayIndexOf(effectiveEnd(ev) - 1); // inclusive last covered day
      return {
        ev,
        startIdx: Math.max(0, rawStart),
        endIdx: Math.min(nDays - 1, rawEnd),
        clipStart: rawStart < 0,          // continues before the window
        clipEnd: rawEnd > nDays - 1,      // continues past the window
      };
    })
    .filter((b) => b.endIdx >= 0 && b.startIdx <= nDays - 1 && b.startIdx <= b.endIdx)
    .sort((a, b) => a.startIdx - b.startIdx || b.endIdx - a.endIdx);
  if (bars.length) {
    const laneLastEnd = []; // lane -> last occupied day index
    for (const b of bars) {
      let lane = laneLastEnd.findIndex((end) => end < b.startIdx);
      if (lane === -1) lane = laneLastEnd.length;
      laneLastEnd[lane] = b.endIdx;
      b.lane = lane;
    }
    const row = document.createElement("div");
    row.className = "cal-allday-row";
    row.style.gridTemplateColumns = cols;
    const gutter = document.createElement("div");
    gutter.className = "cal-gutter-label";
    gutter.textContent = "all-day";
    gutter.style.gridRow = "1 / -1";
    row.appendChild(gutter);
    for (const b of bars) {
      const bar = calChip(b.ev, false, b.clipStart, b.clipEnd);
      bar.classList.add("cal-allday-bar");
      bar.style.gridColumn = `${b.startIdx + 2} / ${b.endIdx + 3}`;
      bar.style.gridRow = String(b.lane + 1);
      row.appendChild(bar);
    }
    calendarEl.appendChild(row);
  }

  // scrollable 24h grid: hour gutter + one positioned column per day
  const scroll = document.createElement("div");
  scroll.className = "cal-scroll";
  scroll.style.gridTemplateColumns = cols;
  const hours = document.createElement("div");
  hours.className = "cal-hours";
  for (let h = 1; h < 24; h++) {
    const label = document.createElement("span");
    label.className = "cal-hour-label";
    label.style.top = `${h * calHourPx()}px`;
    label.textContent = `${pad2(h)}:00`;
    hours.appendChild(label);
  }
  scroll.appendChild(hours);

  const now = Date.now() / 1000;
  for (const day of days) {
    const dayStart = day.getTime() / 1000;
    const col = document.createElement("div");
    col.className = "cal-col" + (dateKey(day) === todayKey ? " today" : "");
    col.dataset.ts = String(dayStart);
    const timed = eventsOverlapping(dayStart, dayStart + 86400).filter((e) => !isAllDay(e));
    for (const item of layoutLanes(timed, dayStart, dayStart + 86400)) {
      col.appendChild(calBlock(item, dayStart));
    }
    if (now >= dayStart && now < dayStart + 86400) {
      const line = document.createElement("div");
      line.className = "cal-now";
      line.style.top = `${((now - dayStart) / 3600) * calHourPx()}px`;
      col.appendChild(line);
    }
    col.addEventListener("pointerdown", onGridPointerDown);
    scroll.appendChild(col);
  }
  calendarEl.appendChild(scroll);
  scroll.scrollTop = calScrollTop !== null ? calScrollTop : 7.5 * calHourPx();
  scroll.addEventListener("scroll", () => { calScrollTop = scroll.scrollTop; });
}

function renderMonth() {
  const { from, days } = calWindow();
  const month = calAnchor.getMonth();
  const todayKey = dateKey(new Date());

  const head = document.createElement("div");
  head.className = "cal-month-head";
  for (let i = 0; i < 7; i++) {
    const span = document.createElement("span");
    span.textContent = addDays(from, i).toLocaleDateString([], { weekday: "short" });
    head.appendChild(span);
  }
  const grid = document.createElement("div");
  grid.className = "cal-month-grid";
  for (let i = 0; i < days; i++) {
    const day = addDays(from, i);
    const dayStart = day.getTime() / 1000;
    const dayKey = dateKey(day);
    const cell = document.createElement("div");
    cell.className = "cal-cell"
      + (day.getMonth() !== month ? " other-month" : "")
      + (dayKey === todayKey ? " today" : "");
    cell.dataset.ts = String(dayStart);

    const num = document.createElement("button");
    num.type = "button";
    num.className = "cal-cell-day";
    num.textContent = day.getDate();
    num.title = "Open day view";
    num.addEventListener("click", (e) => { e.stopPropagation(); switchToDay(day); });
    cell.appendChild(num);

    const list = eventsOverlapping(dayStart, dayStart + 86400)
      .sort((a, b) => isAllDay(b) - isAllDay(a) || a.start - b.start);
    for (const ev of list.slice(0, 3)) {
      const startsToday = dateKey(new Date(ev.start * 1000)) === dayKey;
      const endsToday = dateKey(new Date((effectiveEnd(ev) - 1) * 1000)) === dayKey;
      // time only on the day it starts; ‹ › mark spill from/into other days
      cell.appendChild(calChip(ev, startsToday, !startsToday, !endsToday));
    }
    if (list.length > 3) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "cal-more";
      more.textContent = `+${list.length - 3} more`;
      more.title = "Open day view";
      more.addEventListener("click", (e) => { e.stopPropagation(); switchToDay(day); });
      cell.appendChild(more);
    }
    cell.addEventListener("pointerdown", onMonthPointerDown);
    grid.appendChild(cell);
  }
  calendarEl.append(head, grid);
}

/* ---- drag across month cells -> an all-day event (month is day-granular,
   so both a click and a drag here make whole-day events) ---- */

function onMonthPointerDown(e) {
  if (e.button !== 0
      || e.target.closest(".cal-chip")
      || e.target.closest(".cal-cell-day")
      || e.target.closest(".cal-more")) return;
  // Mouse only — see the note on onGridPointerDown. A touch on the calendar
  // creates nothing; the + button is the only way in.
  if (e.pointerType !== "mouse") return;
  const startTs = Number(e.currentTarget.dataset.ts);
  let endTs = startTs, moved = false;
  const cells = [...calendarEl.querySelectorAll(".cal-cell")];
  const paint = () => {
    const lo = Math.min(startTs, endTs), hi = Math.max(startTs, endTs);
    for (const c of cells) {
      const t = Number(c.dataset.ts);
      c.classList.toggle("drag-range", t >= lo && t <= hi);
    }
  };
  const move = (ev2) => {
    const cell = document.elementFromPoint(ev2.clientX, ev2.clientY)?.closest(".cal-cell");
    if (cell && calendarEl.contains(cell) && Number(cell.dataset.ts) !== endTs) {
      endTs = Number(cell.dataset.ts);
      moved = true;
      paint();
    }
  };
  const up = () => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    calDragging = false;
    for (const c of cells) c.classList.remove("drag-range");
    const lo = Math.min(startTs, endTs), hi = Math.max(startTs, endTs);
    // all-day, covering [lo .. hi] inclusive -> exclusive end = day after hi
    const endExclusive = moved ? addDays(new Date(hi * 1000), 1).getTime() / 1000 : null;
    openEventDialog(null, lo, endExclusive, true);
  };
  calDragging = true;
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

/* ---- drag-to-create on the time grid ----
   The drag can start in one day column and end in another (week view), so a
   dragged span becomes a multi-day *timed* event. Times snap to 15 min. */

function gridYToMin(col, clientY) {
  const rect = col.getBoundingClientRect();
  const min = ((clientY - rect.top) / calHourPx()) * 60;
  return Math.max(0, Math.min(1440, Math.round(min / CAL_SNAP_MIN) * CAL_SNAP_MIN));
}

// absolute snapped time under the pointer: pick the day column by X, minutes
// within it by Y (clamped to that day)
function gridTimeAt(clientX, clientY) {
  const cols = [...calendarEl.querySelectorAll(".cal-col")];
  let col = cols.find((c) => {
    const r = c.getBoundingClientRect();
    return clientX >= r.left && clientX < r.right;
  });
  if (!col) { // left/right of the grid — clamp to the first/last day
    const firstLeft = cols[0].getBoundingClientRect().left;
    col = clientX < firstLeft ? cols[0] : cols[cols.length - 1];
  }
  return Number(col.dataset.ts) + gridYToMin(col, clientY) * 60;
}

function clearDragGhosts() {
  calendarEl.querySelectorAll(".cal-ghost").forEach((g) => g.remove());
}

// preview the [startTs, endTs) span as a clipped ghost in every column it touches
function renderDragGhost(startTs, endTs) {
  clearDragGhosts();
  calendarEl.querySelectorAll(".cal-col").forEach((c) => {
    const dayStart = Number(c.dataset.ts);
    const top = Math.max(startTs, dayStart);
    const bottom = Math.min(endTs, dayStart + 86400);
    if (bottom <= top) return;
    const g = document.createElement("div");
    g.className = "cal-ghost";
    g.style.top = `${((top - dayStart) / 3600) * calHourPx()}px`;
    g.style.height = `${Math.max(((bottom - top) / 3600) * calHourPx(), 4)}px`;
    c.appendChild(g);
  });
}

function onGridPointerDown(e) {
  if (e.button !== 0 || e.target.closest(".cal-event")) return;
  /* MOUSE ONLY, deliberately. The grid is a 672px-tall scroll surface, so on a
     phone a finger lands on it constantly just getting down the page — and
     every one of those contacts used to open the new-event dialog. There is no
     gesture that separates "I meant to create an event here" from "I was
     scrolling", because the useful discriminator on a desktop is the drag, and
     a drag on touch is the scroll. A tap-to-create with no drag is all
     downside: it fires on the accidents and still cannot express a duration.
     So touches on the grid do nothing at all. Creating is the + button;
     tapping an existing .cal-event / .cal-chip / .cal-allday-bar still opens
     it for inspection, since those are `click` handlers elsewhere and are
     specific enough not to be hit by accident. */
  if (e.pointerType !== "mouse") return;
  const t0 = gridTimeAt(e.clientX, e.clientY);
  let t1 = t0, moved = false;
  calDragging = true;
  const move = (ev2) => {
    t1 = gridTimeAt(ev2.clientX, ev2.clientY);
    if (t1 !== t0) moved = true;
    if (moved) renderDragGhost(Math.min(t0, t1), Math.max(t0, t1));
  };
  const up = () => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    clearDragGhosts();
    calDragging = false;
    let start = Math.min(t0, t1), end = Math.max(t0, t1);
    if (!moved || end - start < CAL_SNAP_MIN * 60) { // plain click -> 1h block
      start = t0;
      end = t0 + 3600;
    }
    openEventDialog(null, start, end);
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

/* ---- event dialog (add + detail/edit) ---- */

function setDialogCategory(cat) {
  document.querySelectorAll("#event-category .cat-btn").forEach((b) =>
    b.classList.toggle("active", (b.dataset.cat || null) === cat));
}

function dialogCategory() {
  const active = document.querySelector("#event-category .cat-btn.active");
  return active && active.dataset.cat ? active.dataset.cat : null;
}

document.querySelectorAll("#event-category .cat-btn").forEach((btn) =>
  btn.addEventListener("click", () => setDialogCategory(btn.dataset.cat || null)));

// switch the start/end inputs between datetime-local and date, preserving the
// day. For all-day, "Ends" is the (inclusive) last day; blank = single day.
function setDialogAllDay(on) {
  eventAllday.checked = on;
  for (const inp of [eventStart, eventEnd]) {
    const cur = inp.value;
    if (on) {
      inp.type = "date";
      if (cur) inp.value = cur.slice(0, 10);
    } else {
      inp.type = "datetime-local";
      if (cur && cur.length === 10) inp.value = `${cur}T${inp === eventStart ? "09:00" : "10:00"}`;
    }
  }
}

eventAllday.addEventListener("change", () => setDialogAllDay(eventAllday.checked));

// ev = existing occurrence (detail/edit mode) or null; startTs/endTs prefill a
// new event's bounds (from a drag, a grid/cell click, or the + button);
// allDayHint opens a new event as all-day (month-view interactions).
function openEventDialog(ev, startTs, endTs, allDayHint) {
  dialogEvent = ev || null;
  const allDay = ev ? ev.all_day : !!allDayHint;
  document.getElementById("event-dialog-title").textContent = ev ? "Event" : "New event";
  eventTitle.value = ev ? ev.title : "";
  setDialogCategory(ev ? ev.category : null);

  // set input types first, then values in the matching format. A recurring
  // occurrence carries its own times; the form edits the series definition.
  eventStart.type = eventEnd.type = allDay ? "date" : "datetime-local";
  eventAllday.checked = allDay;
  const startSrc = ev ? ev.series_start : startTs;
  const endSrc = ev ? ev.series_end : endTs;
  if (allDay) {
    eventStart.value = dateKey(new Date(startSrc * 1000));
    // stored end is the exclusive day-after; show the inclusive last day
    eventEnd.value = endSrc ? dateKey(new Date((endSrc - 1) * 1000)) : "";
  } else {
    eventStart.value = toLocalInput(startSrc);
    eventEnd.value = endSrc ? toLocalInput(endSrc) : "";
  }
  eventRecurrence.value = ev ? ev.recurrence : "none";
  eventNotes.value = (ev && ev.notes) || "";
  document.getElementById("event-submit").textContent = ev ? "Save changes" : "Add event";
  const del = document.getElementById("event-delete");
  del.hidden = !ev;
  del.textContent = ev && ev.recurrence !== "none" ? "Delete series" : "Delete";
  eventNote.textContent = ev && ev.recurrence !== "none" ? "Changes apply to the whole series." : "";
  eventNote.className = "save-note";

  /* Imported events are mirrors of Apple Calendar: an edit here would be
     silently undone by the next sync and a delete would resurrect it, so the
     form is shown read-only rather than letting you make a change that will
     not stick. The API refuses these too (409) — this is the polite half. */
  const imported = !!(ev && ev.source === "caldav");
  eventOverlay.classList.toggle("dialog-readonly", imported);
  [eventTitle, eventStart, eventEnd, eventNotes, eventRecurrence, eventAllday]
    .forEach((f) => { if (f) f.disabled = imported; });
  document.querySelectorAll("#event-dialog .cat-btn")
    .forEach((b) => { b.disabled = imported; });
  document.getElementById("event-submit").hidden = imported;
  if (imported) {
    del.hidden = true;
    eventNote.textContent = "From Apple Calendar — edit it there and it will "
      + "update here on the next sync.";
    eventNote.className = "save-note";
  }
  eventOverlay.hidden = false;
  document.body.style.overflow = "hidden";
  eventTitle.focus();
}

function closeEventDialog() {
  eventOverlay.hidden = true;
  dialogEvent = null;
  if (overlayEl.hidden && settingsOverlay.hidden && unlockOverlay.hidden && sleepOverlay.hidden) {
    document.body.style.overflow = "";
  }
}

document.getElementById("event-close").addEventListener("click", closeEventDialog);
document.getElementById("event-backdrop").addEventListener("click", closeEventDialog);

// + button: next full hour when looking at today, 09:00 on the anchored day otherwise
document.getElementById("event-add-btn").addEventListener("click", () => {
  let d;
  if (dateKey(calAnchor) === dateKey(new Date())) {
    d = new Date(Date.now() + 3600e3);
    d.setMinutes(0, 0, 0);
  } else {
    d = startOfDay(calAnchor);
    d.setHours(9);
  }
  openEventDialog(null, d.getTime() / 1000, d.getTime() / 1000 + 3600);
});

eventForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const allDay = eventAllday.checked;
  // all-day end is the inclusive last day in the input; the API wants the
  // exclusive day-after, and a same/earlier day means a single day (no end)
  let end = eventEnd.value || null;
  if (allDay && end) {
    end = end > eventStart.value ? dateKey(addDays(new Date(`${end}T00:00`), 1)) : null;
  }
  const body = {
    title: eventTitle.value,
    start: eventStart.value,       // local ISO string — backend parses it
    end,
    notes: eventNotes.value || null,
    recurrence: eventRecurrence.value,
    category: dialogCategory(),
    all_day: allDay,
  };
  try {
    if (dialogEvent) await putJSON(`/api/events/${dialogEvent.id}`, body);
    else await postJSON("/api/events", body);
    closeEventDialog();
    loadEvents();
  } catch (err) {
    eventNote.textContent = err.message || "Couldn't save the event — is the backend up?";
    eventNote.className = "save-note err";
  }
});

document.getElementById("event-delete").addEventListener("click", async () => {
  if (!dialogEvent) return;
  try {
    await deleteJSON(`/api/events/${dialogEvent.id}`);
  } catch (err) {
    eventNote.textContent = err.message || "Couldn't delete the event — is the backend up?";
    eventNote.className = "save-note err";
    return;
  }
  closeEventDialog();
  loadEvents();
});

/* ---- to-do list ---- */

// whole days a task is past due (due dates are local "YYYY-MM-DD" strings;
// noon-to-noon dodges DST off-by-ones)
function daysOverdue(dueDate) {
  return Math.round((new Date(`${dateKey(new Date())}T12:00`)
    - new Date(`${dueDate}T12:00`)) / 86400e3);
}

function dueChip(task) {
  if (!task.due_date) return null;
  const chip = document.createElement("span");
  chip.className = "due-chip";
  const today = dateKey(new Date());
  if (!task.done && task.due_date < today) {
    const days = daysOverdue(task.due_date);
    chip.textContent = days === 1 ? "1 day overdue" : `${days} days overdue`;
    chip.classList.add("overdue");
  } else if (task.due_date === today) {
    chip.textContent = "due today";
  } else if (task.due_date === dateKey(new Date(Date.now() + 86400e3))) {
    chip.textContent = "due tomorrow";
  } else {
    chip.textContent = `due ${new Date(`${task.due_date}T12:00`)
      .toLocaleDateString([], { day: "numeric", month: "short" })}`;
  }
  return chip;
}

// edit/delete pair on a task row; API data goes in via textContent only
function rowActions(editTitle, onEdit, deleteTitle, onDelete) {
  const wrap = document.createElement("span");
  wrap.className = "row-actions";
  for (const [label, title, cls, handler] of [
    ["✎", editTitle, "row-btn", onEdit],
    ["×", deleteTitle, "row-btn danger", onDelete],
  ]) {
    const btn = document.createElement("button");
    btn.className = cls;
    btn.textContent = label;
    btn.title = title;
    btn.setAttribute("aria-label", title);
    btn.addEventListener("click", handler);
    wrap.appendChild(btn);
  }
  return wrap;
}

// One task row. `widget` owns the add/edit form this row's ✎ reopens and the
// note line errors surface on, so a row always talks to its own list's widget.
function taskRow(task, widget) {
  const li = document.createElement("li");
  li.className = "task-row" + (task.done ? " done" : "");
  const check = document.createElement("button");
  check.className = "task-check";
  check.setAttribute("role", "checkbox");
  check.setAttribute("aria-checked", String(task.done));
  check.title = task.done ? "Mark as not done" : "Mark done";
  check.setAttribute("aria-label", `${task.title} — ${check.title}`);
  check.addEventListener("click", async () => {
    check.disabled = true;
    try {
      if (task.done) await putJSON(`/api/tasks/${task.id}`, { done: false });
      else await postJSON(`/api/tasks/${task.id}/complete`);
      await loadTasks();
    } catch {
      widget.setError("Couldn't update the task — is the backend up?");
      check.disabled = false;
    }
  });
  const main = document.createElement("div");
  main.className = "task-main";
  const title = document.createElement("span");
  title.className = "task-title";
  title.textContent = task.title;
  main.appendChild(title);
  const due = dueChip(task);
  if (due) main.appendChild(due);
  if (task.priority !== "medium") {
    const chip = document.createElement("span");
    chip.className = `prio-chip ${task.priority}`;
    chip.textContent = task.priority.toUpperCase();
    main.appendChild(chip);
  }
  li.append(check, main, rowActions(
    "Edit task", () => widget.openForm(task),
    "Delete task", async () => {
      try { await deleteJSON(`/api/tasks/${task.id}`); } catch { return; }
      if (widget.editingId === task.id) widget.closeForm();
      loadTasks();
    },
  ));
  return li;
}

// which widget a task belongs to: "university" is an exact match, "life" is the
// catch-all so untagged / legacy-tagged tasks always have a home and never vanish
function taskInList(task, listName) {
  return listName === "university"
    ? task.list === "university"
    : task.list !== "university";
}

// The two to-do widgets, top to bottom. `list` is the value written to a task's
// `list` column; keep in sync with the CATEGORIES-style split in taskInList.
const TASK_LISTS = [
  { list: "life", title: "Life stuff" },
  { list: "university", title: "University" },
];

// Priority segmented control — silkscreen chips (Low/Med/High) in place of a
// native <select>, matching the category picker's look. Exposes a .value
// getter/setter so the form code reads/writes it like the old <select>.
function segmentedPriority(group) {
  const btns = [...group.querySelectorAll(".prio-seg-btn")];
  const paint = (v) => btns.forEach((b) => {
    const on = b.dataset.prio === v;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", String(on));
  });
  btns.forEach((b) => b.addEventListener("click", () => paint(b.dataset.prio)));
  return {
    get value() { return (btns.find((b) => b.classList.contains("active")) || btns[1]).dataset.prio; },
    set value(v) { paint(v || "medium"); },
  };
}

// A self-contained to-do widget: one list's card, count, + button, add/edit
// row and done toggle. Each keeps its own editingId / showDone; they share only
// the global plannerTasks array and loadTasks().
function makeTaskWidget({ list, title }) {
  const card = document.getElementById("task-card-tpl")
    .content.firstElementChild.cloneNode(true);
  card.dataset.list = list;
  const q = (sel) => card.querySelector(sel);
  const titleEl = q(".task-card-title");
  const countEl = q(".task-count");
  const addBtn = q(".task-add-btn");
  const form = q(".task-form");
  const titleInput = q(".task-title-input");
  const dueInput = q(".task-due");
  const prioInput = segmentedPriority(q(".prio-seg"));
  const submitBtn = q(".task-submit");
  const note = q(".task-note");
  const listEl = q(".task-list");
  const doneToggle = q(".done-toggle");

  titleEl.textContent = title;
  addBtn.setAttribute("aria-label", `Add ${title} task`);
  addBtn.title = `Add ${title.toLowerCase()} task`;

  const widget = { list, editingId: null, showDone: false };
  widget.setError = (msg) => {
    note.textContent = msg;
    note.className = "save-note err";
    form.hidden = false; // make sure the note (inside the form) is visible
  };

  // the inline row behind the + button doubles as the edit row (✎ on a task)
  widget.openForm = (task) => {
    widget.editingId = task ? task.id : null;
    form.hidden = false;
    addBtn.setAttribute("aria-expanded", "true");
    titleInput.value = task ? task.title : "";
    dueInput.value = (task && task.due_date) || "";
    prioInput.value = task ? task.priority : "medium";
    submitBtn.textContent = task ? "Save" : "Add";
    note.textContent = "";
    note.className = "save-note";
    titleInput.focus();
  };
  widget.closeForm = () => {
    form.hidden = true;
    form.reset();
    widget.editingId = null;
    addBtn.setAttribute("aria-expanded", "false");
  };

  widget.render = () => {
    listEl.textContent = "";
    const mine = plannerTasks.filter((t) => taskInList(t, list));
    const open = mine.filter((t) => !t.done);
    const doneCount = mine.length - open.length;
    countEl.textContent = open.length ? `${open.length} open` : "";
    doneToggle.textContent = widget.showDone
      ? `Hide done (${doneCount})` : `Show done (${doneCount})`;
    doneToggle.setAttribute("aria-pressed", String(widget.showDone));
    doneToggle.hidden = doneCount === 0;

    // flat list in the API's order: open first, then due date, then priority
    const visible = widget.showDone ? mine : open;
    if (!visible.length) {
      const li = document.createElement("li");
      li.className = "log-empty";
      li.textContent = "Nothing to do — add a task with +.";
      listEl.appendChild(li);
      return;
    }
    for (const t of visible) listEl.appendChild(taskRow(t, widget));
  };

  addBtn.addEventListener("click", () => {
    if (form.hidden) widget.openForm(null);
    else widget.closeForm();
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = {
      title: titleInput.value,
      due_date: dueInput.value || null,
      priority: prioInput.value,
    };
    try {
      if (widget.editingId) {
        // list stays whatever the task already had — editing never moves lists
        await putJSON(`/api/tasks/${widget.editingId}`, body);
        widget.closeForm();
      } else {
        // new tasks land in this widget's list
        await postJSON("/api/tasks", { ...body, list });
        titleInput.value = ""; // stay open for the next quick add
        titleInput.focus();
      }
      loadTasks();
    } catch (err) {
      widget.setError(err.message || "Couldn't save the task — is the backend up?");
    }
  });

  doneToggle.addEventListener("click", () => {
    widget.showDone = !widget.showDone;
    widget.render();
  });

  document.getElementById("todo-zone").appendChild(card);
  return widget;
}

const taskWidgets = TASK_LISTS.map(makeTaskWidget);

// re-render every to-do widget from the shared plannerTasks array
function renderTasks() {
  for (const w of taskWidgets) w.render();
}

/* ------------------------------------------------------------------ time */

function axisTime(ts, spanSeconds) {
  const d = new Date(ts * 1000);
  if (spanSeconds > 36 * 3600) {
    return d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function relTime(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 50) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/* ----------------------------------------------------------------- start */

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    refreshAllSparks();
    if (openWidget) loadDetail(openWidget);
  }, 200);
});

/* ------------------------------------------------------------------ health
   Health tab (pass 7): last night's Recovery + Sleep scores with their
   contributor breakdowns (actual values against baseline, not just bars),
   vitals-vs-baseline delta tiles, and score trends on the board; the deeper
   analysis lives in two dialogs — the Sleep card opens the sleep detail
   (hypnogram + durations, restorative, tonight's target, debt, consistency),
   a vitals tile opens that vital's nightly history against its baseline
   band. Structure follows the reference screens; the look is this hub's own
   soldermask language. Data from /api/health/night and /api/health/history;
   refreshed on view switch, since health data changes about once a day. */

const HEALTH_STAGE_LABELS = { awake: "Awake", rem: "REM", core: "Core", deep: "Deep" };
const STAGE_LANES = ["awake", "rem", "core", "deep"];
const FLAG_LABELS = { temp_deviation: "Temp deviation", spo2_dip: "SpO₂ dip", rr_spike: "Resp spike" };
const RECOVERY_DRIVERS = { hrv: "HRV", rhr: "Resting HR", rr: "Respiration" };
const SLEEP_DRIVERS = [["duration", "Duration"], ["waso", "Wake time"],
  ["consistency", "Consistency"], ["rem", "REM"], ["awakenings", "Awakenings"], ["deep", "Deep"]];
// per-vital display + history config: where the nightly value lives in
// /api/health/history rows, which baseline metric scores it, and which
// direction is good (colors the deltas; RHR/resp are inverted in the score
// the same way — lower = better — so green here never disagrees with it)
const VITAL_DEFS = {
  hrv:        { label: "HRV", unit: "ms", field: "rmssd", base: "ln_rmssd", exp: true, digits: 0, good: "up" },
  rhr:        { label: "Resting HR", unit: "bpm", field: "rhr", base: "rhr", digits: 0, good: "down" },
  resp_rate:  { label: "Respiration", unit: "br/min", field: "resp_rate", base: "resp_rate", digits: 1, good: "down" },
  spo2:       { label: "SpO₂", unit: "%", field: "spo2", base: "spo2", digits: 1, good: "up" },
  wrist_temp: { label: "Wrist temp", unit: "°C", field: "wrist_temp", base: "wrist_temp", digits: 1, good: "flat" },
};
let healthRange = "30d";
let healthNight = null;   // the night the check-in rating applies to
let healthData = null;    // cached /api/health/night readout (dialogs read it)
let healthHist = null;    // cached 60d /api/health/history (sliced per range)

const NS_SVG = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const el = document.createElementNS(NS_SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

function fmtDur(min) {
  if (min == null) return "—";
  const sign = min < 0 ? "−" : "";
  const m = Math.round(Math.abs(min));
  return `${sign}${Math.floor(m / 60)}h ${m % 60}m`;
}
function scoreColor(v) {
  if (v == null) return cssVar("--muted");
  if (v >= 67) return cssVar("--good");
  if (v >= 34) return cssVar("--copper");
  return cssVar("--critical");
}
function stageColor(stage) {
  return { awake: cssVar("--critical"), rem: "#5cc6e0",
           core: cssVar("--s-temp"), deep: cssVar("--s-co2") }[stage] || cssVar("--muted");
}
const recoveryWord = (v) => v == null ? "—" : v >= 67 ? "Recovered" : v >= 34 ? "Moderate" : "Fatigued";
const sleepWord = (v) => v == null ? "—" : v >= 67 ? "Strong" : v >= 34 ? "Fair" : "Poor";
const vitalFmt = (v) => Math.abs(v) >= 100 ? String(Math.round(v)) : String(Math.round(v * 10) / 10);

function fmtNightLabel(night) {
  const d = new Date(night + "T00:00");
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

async function refreshHealth() {
  const empty = document.getElementById("health-empty");
  const body = document.getElementById("health-body");
  const msg = document.getElementById("health-empty-msg");
  let data;
  try {
    data = await getJSON("/api/health/night");
  } catch {
    empty.hidden = false; body.hidden = true;
    msg.textContent = "Backend unreachable.";
    return;
  }
  if (!data.night) {
    empty.hidden = false; body.hidden = true;
    msg.textContent = "No health data yet — point Health Auto Export at POST /api/health/ingest.";
    return;
  }
  empty.hidden = true; body.hidden = false;
  healthNight = data.night;
  healthData = data;
  healthHist = null; // stale after new data — dialogs refetch on open
  document.getElementById("health-night").textContent = fmtNightLabel(data.night);
  renderScoreCard(data, "recovery");
  renderScoreCard(data, "sleep");
  renderVitals(data);
  renderCheckin(data.subjective);
  loadHealthTrend();
  loadHealthCorrelation();
}

// 60d history feeds every dialog chart; fetched once, sliced per range
async function ensureHealthHist() {
  if (!healthHist) healthHist = (await getJSON("/api/health/history?range=60d")).nights;
  return healthHist;
}
function lastNights(nights, rangeStr) {
  const days = parseInt(rangeStr, 10);
  const cut = addDays(startOfDay(new Date()), -(days - 1));
  const cutIso = `${cut.getFullYear()}-${pad2(cut.getMonth() + 1)}-${pad2(cut.getDate())}`;
  return (nights || []).filter((n) => n.night >= cutIso);
}

// pass 8: subjective morning rating (highlight the logged value) + how well
// the computed scores track it
function renderCheckin(subjective) {
  const rating = subjective ? subjective.rating : null;
  document.querySelectorAll("#rating-row .rating-btn").forEach((btn) =>
    btn.classList.toggle("chosen", Number(btn.dataset.rating) === rating));
}

async function loadHealthCorrelation() {
  const note = document.getElementById("checkin-corr");
  try {
    const c = await getJSON("/api/health/correlation");
    if (c.recovery.r == null && c.sleep.r == null) {
      note.textContent = c.ratings
        ? `${c.ratings} night${c.ratings === 1 ? "" : "s"} rated — a few more and I can show how well the scores track your feel.`
        : "Rate a few mornings to see how well the scores track how you actually feel.";
      return;
    }
    const parts = [];
    if (c.recovery.r != null) parts.push(`Recovery r=${c.recovery.r.toFixed(2)}`);
    if (c.sleep.r != null) parts.push(`Sleep r=${c.sleep.r.toFixed(2)}`);
    note.textContent = `Your feel vs the scores over ${c.ratings} nights: ${parts.join(", ")}. Higher r = the model tracks you better.`;
  } catch {
    note.textContent = "";
  }
}

async function submitRating(rating) {
  try {
    await postJSON("/api/health/subjective", { rating, night: healthNight });
    renderCheckin({ rating });
    loadHealthCorrelation();
  } catch { /* leave the prior selection */ }
}

document.querySelectorAll("#rating-row .rating-btn").forEach((btn) =>
  btn.addEventListener("click", () => submitRating(Number(btn.dataset.rating))));

// one score card (recovery or sleep): big number, status, fill, driver bars
function renderScoreCard(data, kind) {
  const isRec = kind === "recovery";
  const s = isRec ? data.score : data.sleep;
  const value = s && (isRec ? s.recovery : s.sleep_score);
  const num = document.getElementById(`${kind}-num`);
  const fill = document.getElementById(`${kind}-fill`);
  const status = document.getElementById(`${kind}-status`);
  const drivers = document.getElementById(`${kind}-drivers`);
  const why = document.getElementById(`${kind}-why`);
  drivers.textContent = "";
  if (value == null) {
    num.textContent = "—"; num.style.color = ""; fill.style.width = "0";
    status.textContent = "Not enough data yet"; why.textContent = "";
    return;
  }
  const v = Math.round(value);
  num.textContent = v; num.style.color = scoreColor(v);
  fill.style.width = `${v}%`; fill.style.background = scoreColor(v);
  status.textContent = (isRec ? recoveryWord(v) : sleepWord(v)) + (s.provisional ? " · provisional" : "");
  status.classList.toggle("provisional", !!s.provisional);

  if (isRec) {
    // each driver carries its actual value + baseline, not just the z bar
    const m = data.metrics, b = data.baselines;
    const actuals = {
      hrv: [m.rmssd, "ms", b.ln_rmssd && b.ln_rmssd.mean != null ? Math.exp(b.ln_rmssd.mean) : null],
      rhr: [m.rhr, "bpm", b.rhr && b.rhr.mean],
      rr: [m.resp_rate, "br/min", b.resp_rate && b.resp_rate.mean],
    };
    for (const key of ["hrv", "rhr", "rr"]) {
      const c = s.contributions[key];
      const [value, unit, baseline] = actuals[key];
      drivers.appendChild(driverRow(RECOVERY_DRIVERS[key], c ? c.z : null,
                                    value, unit, baseline));
    }
    if (s.flags && s.flags.length) {
      const row = document.createElement("div");
      row.className = "score-flags";
      for (const f of s.flags) {
        const chip = document.createElement("span");
        chip.className = "flag-chip";
        chip.textContent = FLAG_LABELS[f] || f;
        row.appendChild(chip);
      }
      drivers.appendChild(row);
    }
    why.textContent = recoveryNarrative(data);
  } else {
    for (const [key, label] of SLEEP_DRIVERS) {
      const sub = s.subscores[key];
      driversAppendSleep(drivers, label, sub ? sub.value : null);
    }
    why.textContent = sleepNarrative(data);
  }
}

// recovery driver: diverging bar laid out by the RAW deviation — a reading
// above baseline always extends right, below always left — while the colour
// carries the health direction (the backend's sign-adjusted z: green =
// better than baseline, red = worse). So a LOWER resting HR draws to the
// left but in green, matching the vitals tiles' bars. Also shows the actual
// value and its % against the baseline.
function driverRow(label, z, value, unit, baseline) {
  const row = document.createElement("div");
  row.className = "driver-row has-reading";
  const name = document.createElement("span");
  name.className = "driver-name"; name.textContent = label;
  const bar = document.createElement("span");
  bar.className = "driver-bar diverging";
  const bfill = document.createElement("span");
  bfill.className = "driver-fill";
  if (z != null) {
    const mag = Math.min(Math.abs(z) / 2.5, 1) * 50;
    const raw = value != null && baseline != null ? value - baseline : z;
    bfill.style.width = `${mag}%`;
    bfill.style.background = cssVar(z >= 0 ? "--good" : "--critical");
    if (raw >= 0) bfill.style.left = "50%"; else bfill.style.right = "50%";
  }
  bar.appendChild(bfill);
  const val = document.createElement("span");
  val.className = "driver-val";
  if (value == null) {
    val.textContent = "—";
  } else {
    const reading = document.createElement("span");
    reading.className = "driver-reading";
    reading.innerHTML = `${vitalFmt(value)}<span class="vital-unit">${unit}</span>`;
    val.appendChild(reading);
    if (baseline != null && baseline !== 0) {
      const pct = Math.round((value / baseline - 1) * 100);
      const vs = document.createElement("span");
      vs.className = `driver-vs ${z == null ? "" : z >= 0 ? "good" : "bad"}`;
      vs.textContent = `${pct >= 0 ? "+" : "−"}${Math.abs(pct)}% (${vitalFmt(baseline)})`;
      val.appendChild(vs);
    }
  }
  row.append(name, bar, val);
  return row;
}

// 0-100 sub-score bar (copper fill) for the sleep card
function driversAppendSleep(drivers, label, value) {
  const row = document.createElement("div");
  row.className = "driver-row";
  const name = document.createElement("span");
  name.className = "driver-name"; name.textContent = label;
  const bar = document.createElement("span");
  bar.className = "driver-bar";
  const bfill = document.createElement("span");
  bfill.className = "driver-fill";
  if (value != null) {
    bfill.style.left = "0";
    bfill.style.width = `${Math.max(0, Math.min(100, value))}%`;
    bfill.style.background = cssVar("--copper");
  }
  bar.appendChild(bfill);
  const val = document.createElement("span");
  val.className = "driver-val";
  val.textContent = value == null ? "—" : Math.round(value);
  row.append(name, bar, val);
  drivers.appendChild(row);
}

function recoveryNarrative(data) {
  const m = data.metrics, b = data.baselines;
  if (m.rmssd == null || !b.ln_rmssd || b.ln_rmssd.mean == null) return "";
  const base = Math.exp(b.ln_rmssd.mean);
  const pct = Math.round((m.rmssd / base - 1) * 100);
  return `HRV ${Math.round(m.rmssd)} ms vs your ${Math.round(base)} ms baseline (${pct >= 0 ? "+" : ""}${pct}%).`;
}
function sleepNarrative(data) {
  const m = data.metrics, sl = data.sleep;
  if (m.tst_min == null) return "";
  let s = `${fmtDur(m.tst_min)} asleep`;
  if (sl && sl.target_sleep_min != null) s += ` · target ${fmtDur(sl.target_sleep_min)}`;
  if (m.waso_min != null) s += ` · ${fmtDur(m.waso_min)} awake`;
  return `${s}.`;
}

// the delta vs baseline is the headline (it, not the absolute value, is what
// the recovery model scores); the reading + baseline ride below, and a small
// diverging bar redraws the deviation. Each tile opens the history dialog.
function renderVitals(data) {
  const grid = document.getElementById("vitals-grid");
  grid.textContent = "";
  const m = data.metrics, b = data.baselines;
  for (const [key, def] of Object.entries(VITAL_DEFS)) {
    const base = b[def.base] && b[def.base].mean != null
      ? (def.exp ? Math.exp(b[def.base].mean) : b[def.base].mean) : null;
    grid.appendChild(vitalTile(key, def, m[def.field], base));
  }
}
function vitalTile(key, def, value, baseline) {
  const tile = document.createElement("div");
  tile.className = "vital-tile";
  tile.setAttribute("role", "button");
  tile.setAttribute("tabindex", "0");
  tile.setAttribute("aria-label", `${def.label} — open history`);
  const lab = document.createElement("span");
  lab.className = "vital-label"; lab.textContent = def.label;
  const big = document.createElement("span");
  big.className = "vital-delta-big";
  const reading = document.createElement("span");
  reading.className = "vital-reading";
  const devbar = document.createElement("span");
  devbar.className = "vital-devbar";
  const devfill = document.createElement("span");
  devfill.className = "vital-devfill";
  devbar.appendChild(devfill);

  if (value == null) {
    big.textContent = "—"; big.classList.add("neutral");
    reading.textContent = "no data";
  } else if (baseline == null) {
    big.textContent = "—"; big.classList.add("neutral");
    reading.innerHTML = `${vitalFmt(value)}<span class="vital-unit">${def.unit}</span> · no baseline yet`;
  } else {
    const delta = value - baseline;
    const isTemp = def.good === "flat"; // °C: show the absolute deviation, % of 36° is noise
    const pct = baseline ? delta / baseline * 100 : 0;
    const good = def.good === "up" ? delta >= 0
      : def.good === "down" ? delta <= 0
      : Math.abs(delta) <= 0.5;
    const arrow = delta > 0 ? "↑" : delta < 0 ? "↓" : "→";
    big.textContent = isTemp
      ? `${arrow} ${Math.abs(delta).toFixed(1)}°C`
      : `${arrow} ${Math.abs(pct).toFixed(0)}%`;
    big.classList.add(good ? "good" : "bad");
    reading.innerHTML =
      `${vitalFmt(value)}<span class="vital-unit">${def.unit}</span> vs ${vitalFmt(baseline)}`;
    // deviation bar, centred on the baseline: ±20% (or ±1°C) = full half-width
    const mag = Math.min(Math.abs(isTemp ? delta / 1.0 : pct / 20) , 1) * 50;
    devfill.style.width = `${mag}%`;
    devfill.style.background = cssVar(good ? "--good" : "--critical");
    if (delta >= 0) devfill.style.left = "50%"; else devfill.style.right = "50%";
  }
  tile.append(lab, big, reading, devbar);
  tile.addEventListener("click", () => openVitalsDetail(key));
  tile.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openVitalsDetail(key); }
  });
  return tile;
}

function renderStages(data, box) {
  box.textContent = "";
  const m = data.metrics;
  const total = (m.tst_min || 0) + (m.waso_min || 0);
  if (!total) return;
  const rows = [
    ["awake", "Awake", m.waso_min, null],
    ["rem", "REM", m.rem_min, data.baselines.rem_frac],
    ["core", "Core", m.core_min, null],
    ["deep", "Deep", m.deep_min, data.baselines.deep_frac],
  ];
  for (const [stage, label, min, baseFrac] of rows) {
    const pct = (min || 0) / total * 100;
    const row = document.createElement("div");
    row.className = "stage-row";
    const name = document.createElement("span");
    name.className = "stage-name"; name.textContent = label;
    const pctEl = document.createElement("span");
    pctEl.className = "stage-pct"; pctEl.style.color = stageColor(stage);
    pctEl.textContent = `${Math.round(pct)}%`;
    const track = document.createElement("span");
    track.className = "stage-track";
    const fill = document.createElement("span");
    fill.className = "stage-fill";
    fill.style.width = `${pct}%`; fill.style.background = stageColor(stage);
    track.appendChild(fill);
    // typical marker: baseline stage fraction (of TST) mapped onto the in-bed total
    if (baseFrac && baseFrac.mean != null && m.tst_min) {
      const typ = baseFrac.mean * m.tst_min / total * 100;
      const tick = document.createElement("span");
      tick.className = "stage-typ";
      tick.style.left = `${Math.min(100, typ)}%`;
      track.appendChild(tick);
    }
    const dur = document.createElement("span");
    dur.className = "stage-dur"; dur.textContent = fmtDur(min);
    row.append(name, pctEl, track, dur);
    box.appendChild(row);
  }
}

function renderHypnogram(stages, container) {
  container.textContent = "";
  if (!stages || !stages.length) {
    const e = document.createElement("div");
    e.className = "chart-empty"; e.textContent = "No stage data for this night.";
    container.appendChild(e);
    return;
  }
  const w = container.clientWidth || 320, h = container.clientHeight || 130;
  const padL = 42, padB = 16, padT = 6;
  const t0 = Math.min(...stages.map((s) => s.start));
  const t1 = Math.max(...stages.map((s) => s.end));
  const laneH = (h - padB - padT) / STAGE_LANES.length;
  const X = (t) => padL + (t - t0) / (t1 - t0) * (w - padL - 6);
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}` });
  STAGE_LANES.forEach((st, i) => {
    const y = padT + i * laneH;
    const lbl = svgEl("text", { x: padL - 6, y: y + laneH / 2 + 3,
      fill: cssVar("--muted"), "font-size": 9, "text-anchor": "end" });
    lbl.textContent = HEALTH_STAGE_LABELS[st];
    svg.appendChild(lbl);
  });
  for (const seg of stages) {
    const lane = STAGE_LANES.indexOf(seg.stage);
    if (lane < 0) continue;
    const x = X(seg.start), x2 = X(seg.end);
    svg.appendChild(svgEl("rect", { x, y: padT + lane * laneH + laneH * 0.18,
      width: Math.max(1, x2 - x), height: laneH * 0.64, rx: 2, fill: stageColor(seg.stage) }));
  }
  for (const t of [t0, (t0 + t1) / 2, t1]) {
    const tx = svgEl("text", { x: X(t), y: h - 4, fill: cssVar("--muted"),
      "font-size": 9, "text-anchor": "middle" });
    tx.textContent = fmtHM(t);
    svg.appendChild(tx);
  }
  // hover: which stage is under the cursor, and how long that segment ran
  const segs = [...stages].sort((a, b) => a.start - b.start);
  svg.addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const t = t0 + ((ev.clientX - rect.left) / rect.width * w - padL) / (w - padL - 6) * (t1 - t0);
    const seg = segs.find((s) => s.start <= t && t <= s.end);
    if (!seg) { tooltip.hidden = true; return; }
    healthTip(ev, `${HEALTH_STAGE_LABELS[seg.stage]} · ${fmtDur((seg.end - seg.start) / 60)}`,
              [`${fmtHM(seg.start)} – ${fmtHM(seg.end)}`]);
  });
  svg.addEventListener("pointerleave", () => { tooltip.hidden = true; });
  container.appendChild(svg);
}

// shared tooltip for the health charts: bold headline + plain lines,
// positioned like the board charts' crosshair tooltip
function healthTip(ev, title, lines) {
  tooltip.hidden = false;
  tooltip.textContent = "";
  const strong = document.createElement("span");
  strong.className = "tt-value";
  strong.textContent = title;
  tooltip.appendChild(strong);
  (lines || []).forEach((line, i) => {
    if (i) tooltip.appendChild(document.createElement("br"));
    tooltip.appendChild(document.createTextNode(line));
  });
  const tx = Math.min(ev.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8);
  tooltip.style.left = `${tx}px`;
  tooltip.style.top = `${ev.clientY - 10 - tooltip.offsetHeight}px`;
}

async function loadHealthTrend() {
  const container = document.getElementById("health-trend");
  try {
    const data = await getJSON(`/api/health/history?range=${healthRange}`);
    drawHealthTrend(container, data.nights);
  } catch {
    container.textContent = "";
  }
}

// grouped bars per night: recovery (zone-coloured) + sleep (copper); provisional
// nights dimmed
function drawHealthTrend(container, nights) {
  container.textContent = "";
  const scored = (nights || []).filter((n) => n.recovery != null || n.sleep_score != null);
  if (!scored.length) {
    const e = document.createElement("div");
    e.className = "chart-empty"; e.textContent = "No scored nights in this range yet.";
    container.appendChild(e);
    return;
  }
  const w = container.clientWidth || 320, h = container.clientHeight || 200;
  const pad = { top: 10, right: 8, bottom: 22, left: 26 };
  const iw = w - pad.left - pad.right, ih = h - pad.top - pad.bottom;
  const n = scored.length, slot = iw / n;
  const bw = Math.max(2, Math.min(slot * 0.36, 15));
  const Y = (v) => pad.top + ih - (v / 100) * ih;
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}` });
  for (const g of [0, 50, 100]) {
    svg.appendChild(svgEl("line", { x1: pad.left, y1: Y(g), x2: w - pad.right, y2: Y(g),
      stroke: cssVar("--grid"), "stroke-width": 1 }));
    const t = svgEl("text", { x: pad.left - 5, y: Y(g) + 3, fill: cssVar("--muted"),
      "font-size": 9, "text-anchor": "end" });
    t.textContent = g;
    svg.appendChild(t);
  }
  scored.forEach((nt, i) => {
    const cx = pad.left + slot * (i + 0.5);
    if (nt.recovery != null) {
      const y = Y(nt.recovery);
      svg.appendChild(svgEl("rect", { x: cx - bw - 1, y, width: bw, height: pad.top + ih - y,
        rx: 1, fill: scoreColor(nt.recovery), opacity: nt.rec_provisional ? 0.4 : 1 }));
    }
    if (nt.sleep_score != null) {
      const y = Y(nt.sleep_score);
      svg.appendChild(svgEl("rect", { x: cx + 1, y, width: bw, height: pad.top + ih - y,
        rx: 1, fill: cssVar("--copper"), opacity: nt.sleep_provisional ? 0.4 : 1 }));
    }
  });
  const step = Math.max(1, Math.ceil(n / 6));
  scored.forEach((nt, i) => {
    if (i % step !== 0 && i !== n - 1) return;
    const cx = pad.left + slot * (i + 0.5);
    const d = new Date(nt.night + "T00:00");
    const t = svgEl("text", { x: cx, y: h - 7, fill: cssVar("--muted"),
      "font-size": 9, "text-anchor": "middle" });
    t.textContent = `${d.getDate()}/${d.getMonth() + 1}`;
    svg.appendChild(t);
  });
  // hover: date + both scores for the night under the cursor
  scored.forEach((nt, i) => {
    const hit = svgEl("rect", { x: pad.left + slot * i, y: pad.top,
      width: slot, height: ih, fill: "transparent" });
    hit.addEventListener("pointermove", (ev) => {
      const lines = [];
      if (nt.recovery != null) lines.push(
        `Recovery ${Math.round(nt.recovery)}${nt.rec_provisional ? " ·prov" : ""}`);
      if (nt.sleep_score != null) lines.push(
        `Sleep ${Math.round(nt.sleep_score)}${nt.sleep_provisional ? " ·prov" : ""}`);
      healthTip(ev, fmtNightLabel(nt.night), lines);
    });
    hit.addEventListener("pointerleave", () => { tooltip.hidden = true; });
    svg.appendChild(hit);
  });
  container.appendChild(svg);
}

document.querySelectorAll("#health-range .range-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.hrange === healthRange) return;
    healthRange = btn.dataset.hrange;
    document.querySelectorAll("#health-range .range-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.hrange === healthRange));
    loadHealthTrend();
  });
});

/* ------------------------------------------------- sleep detail dialog
   Opened by the Sleep score card. Sections in the reference screens' order:
   stage trace (hover = stage + segment duration) with the duration split
   below, restorative sleep (ring + stacked deep/REM bars), tonight's target
   (length + bed/wake times worked back from the last week), sleep-debt
   evolution, and sleep-time consistency (bed-to-wake bars + the SRI). */

const sleepdOverlay = document.getElementById("sleepd-overlay");
const vitalsOverlay = document.getElementById("vitals-overlay");
let sleepNeedMin = null; // fetched once, names the debt note's threshold

const meanOf = (arr) => arr.length ? arr.reduce((a, x) => a + x, 0) / arr.length : null;
const nightDate = (night) => new Date(night + "T00:00");
// hours since the noon-to-noon anchor (noon the day BEFORE the wake date) —
// same anchor the backend keys nights on, so bedtimes never wrap at midnight
function hSinceNoon(ts, night) {
  const anchor = nightDate(night).getTime() - 12 * 3600e3;
  return (ts * 1000 - anchor) / 3600e3;
}
function noonHLabel(h) {
  const total = Math.round(((h + 12) % 24 + 24) % 24 * 60);
  return `${pad2(Math.floor(total / 60) % 24)}:${pad2(total % 60)}`;
}
const barHM = (min) => `${Math.floor(min / 60)}:${pad2(Math.round(min) % 60)}`;

// shared scaffold for the per-night charts: one slot per calendar day in the
// range (so a night with no data leaves a visible gap), plus the day axis
function nightChart(container, rangeStr, pad) {
  container.textContent = "";
  const days = parseInt(rangeStr, 10);
  const w = container.clientWidth || 660, h = container.clientHeight || 190;
  const start = addDays(startOfDay(new Date()), -(days - 1));
  const sc = {
    days, w, h, pad,
    iw: w - pad.left - pad.right, ih: h - pad.top - pad.bottom,
    start, slot: (w - pad.left - pad.right) / days,
    svg: svgEl("svg", { viewBox: `0 0 ${w} ${h}` }),
  };
  sc.X = (night) => pad.left +
    (Math.round((nightDate(night) - start) / 86400e3) + 0.5) * sc.slot;
  return sc;
}
function drawNightTicks(sc) {
  const step = sc.days <= 7 ? 1 : Math.ceil(sc.days / 6);
  for (let i = 0; i < sc.days; i += step) {
    const d = addDays(sc.start, i);
    const t = svgEl("text", { x: sc.pad.left + (i + 0.5) * sc.slot, y: sc.h - 5,
      fill: cssVar("--muted"), "font-size": 9, "text-anchor": "middle",
      "font-family": "ui-monospace, Menlo, monospace" });
    t.textContent = sc.days <= 7
      ? d.toLocaleDateString(undefined, { weekday: "short" })
      : `${d.getDate()}/${d.getMonth() + 1}`;
    sc.svg.appendChild(t);
  }
}
function chartEmpty(container, msg) {
  const e = document.createElement("div");
  e.className = "chart-empty"; e.textContent = msg;
  container.appendChild(e);
}
function hitRect(sc, x, onMove) {
  const hit = svgEl("rect", { x, y: sc.pad.top, width: sc.slot,
    height: sc.ih, fill: "transparent" });
  hit.addEventListener("pointermove", onMove);
  hit.addEventListener("pointerleave", () => { tooltip.hidden = true; });
  sc.svg.appendChild(hit);
}

async function openSleepDetail() {
  if (!healthData || !healthData.night) return;
  sleepdOverlay.hidden = false;
  document.body.style.overflow = "hidden";
  document.getElementById("sleepd-title").textContent =
    `Sleep — ${fmtNightLabel(healthData.night)}`;
  renderSleepSummary();
  renderHypnogram(healthData.stages, document.getElementById("sd-hypnogram"));
  renderSdLegend();
  renderStages(healthData, document.getElementById("sd-stages"));
  document.getElementById("sleepd-close").focus();
  try { await ensureHealthHist(); } catch { healthHist = []; }
  if (sleepNeedMin == null) {
    try { sleepNeedMin = (await getJSON("/api/health/settings")).sleep_need_min; } catch { /* note stays generic */ }
  }
  renderResto();
  renderTarget();
  renderDebt();
  renderCons();
}

function closeSleepDetail() {
  sleepdOverlay.hidden = true;
  healthRestoreScroll();
  document.getElementById("sleep-card").focus();
}

function healthRestoreScroll() {
  if (overlayEl.hidden && settingsOverlay.hidden && unlockOverlay.hidden &&
      sleepOverlay.hidden && eventOverlay.hidden &&
      sleepdOverlay.hidden && vitalsOverlay.hidden) {
    document.body.style.overflow = "";
  }
}

function renderSleepSummary() {
  const m = healthData.metrics || {};
  const el = document.getElementById("sd-summary");
  if (m.tst_min == null) { el.textContent = "No sleep-stage data for this night."; return; }
  let s = `${fmtDur(m.tst_min)} asleep`;
  if (m.onset_ts != null && m.wake_ts != null) {
    s += ` · fell asleep ${fmtHM(m.onset_ts)} · woke ${fmtHM(m.wake_ts)}`;
  }
  if (m.waso_min) s += ` · ${fmtDur(m.waso_min)} awake`;
  el.textContent = s;
}

function renderSdLegend() {
  const box = document.getElementById("sd-legend");
  box.textContent = "";
  for (const st of STAGE_LANES) {
    const item = document.createElement("span");
    const dot = document.createElement("span");
    dot.className = "dot"; dot.style.background = stageColor(st);
    item.append(dot, HEALTH_STAGE_LABELS[st]);
    box.appendChild(item);
  }
}

// --- restorative sleep: ring + tonight's minutes, then deep/REM stacks
function renderResto() {
  const sl = healthData.sleep || {}, m = healthData.metrics || {};
  const restoMin = (m.rem_min || 0) + (m.deep_min || 0);
  renderRestoRing(document.getElementById("sd-resto-ring"), sl.restorative_pct);
  document.getElementById("sd-resto-dur").textContent = restoMin ? fmtDur(restoMin) : "—";
  const withResto = (healthHist || []).filter((n) => (n.rem_min || 0) + (n.deep_min || 0) > 0);
  const avgMin = meanOf(withResto.map((n) => (n.rem_min || 0) + (n.deep_min || 0)));
  document.getElementById("sd-resto-avg").textContent =
    avgMin != null ? `average ${fmtDur(avgMin)}` : "";
  const note = document.getElementById("sd-resto-note");
  if (sl.restorative_pct != null) {
    const avgPct = meanOf(withResto.map((n) => n.restorative_pct).filter((v) => v != null));
    const vsAvg = avgPct == null ? "" :
      ` — ${sl.restorative_pct >= avgPct ? "above" : "below"} your ${Math.round(avgPct)}% average`;
    note.textContent = `Deep + REM made up ${Math.round(sl.restorative_pct)}% of tonight's sleep${vsAvg}. A third or more is a good target.`;
  } else {
    note.textContent = "";
  }
  const range = activeRange("sd-resto-range");
  drawRestoChart(document.getElementById("sd-resto-chart"), lastNights(healthHist, range), range);
}

function renderRestoRing(box, pct) {
  box.textContent = "";
  const svg = svgEl("svg", { viewBox: "0 0 72 72" });
  const ring = (attrs) => svg.appendChild(svgEl("circle", {
    cx: 36, cy: 36, r: 30, fill: "none", "stroke-width": 7, ...attrs }));
  ring({ stroke: cssVar("--surface-2") });
  if (pct != null) {
    const len = 2 * Math.PI * 30;
    ring({ stroke: cssVar("--s-co2"), "stroke-linecap": "round",
      "stroke-dasharray": `${len * Math.min(pct, 100) / 100} ${len}`,
      transform: "rotate(-90 36 36)" });
  }
  const t = svgEl("text", { x: 36, y: 41, "text-anchor": "middle", fill: cssVar("--ink"),
    "font-size": 15, "font-weight": 700 });
  t.textContent = pct == null ? "—" : `${Math.round(pct)}%`;
  svg.appendChild(t);
  box.appendChild(svg);
}

function drawRestoChart(container, nights, rangeStr) {
  const data = nights.filter((n) => (n.rem_min || 0) + (n.deep_min || 0) > 0);
  container.textContent = "";
  if (!data.length) return chartEmpty(container, "No restorative-sleep data in this range yet.");
  const sc = nightChart(container, rangeStr, { top: 26, right: 8, bottom: 18, left: 30 });
  const yMax = Math.max(1, ...data.map((n) => ((n.rem_min || 0) + (n.deep_min || 0)) / 60));
  const Y = (hrs) => sc.pad.top + sc.ih - hrs / yMax * sc.ih;
  for (const frac of [0.5, 1]) {
    const y = Y(yMax * frac);
    sc.svg.appendChild(svgEl("line", { x1: sc.pad.left, y1: y, x2: sc.w - sc.pad.right,
      y2: y, stroke: cssVar("--grid"), "stroke-width": 1 }));
    const t = svgEl("text", { x: sc.pad.left - 5, y: y + 3, fill: cssVar("--muted"),
      "font-size": 9, "text-anchor": "end",
      "font-family": "ui-monospace, Menlo, monospace" });
    t.textContent = (yMax * frac).toFixed(1);
    sc.svg.appendChild(t);
  }
  const bw = Math.min(sc.slot * 0.6, 24);
  for (const n of data) {
    const deep = n.deep_min || 0, rem = n.rem_min || 0;
    const x = sc.X(n.night) - bw / 2;
    const yDeep = Y(deep / 60), yTop = Y((deep + rem) / 60), y0 = sc.pad.top + sc.ih;
    if (deep) sc.svg.appendChild(svgEl("rect", { x, y: yDeep, width: bw,
      height: y0 - yDeep, rx: 2, fill: stageColor("deep") }));
    if (rem) sc.svg.appendChild(svgEl("rect", { x, y: yTop, width: bw,
      height: Math.max(0, yDeep - yTop - 2), rx: 2, fill: stageColor("rem") }));
    if (sc.days <= 7) {
      const lbl = svgEl("text", { x: sc.X(n.night), y: yTop - 5, "text-anchor": "middle",
        fill: cssVar("--ink-2"), "font-size": 9,
        "font-family": "ui-monospace, Menlo, monospace" });
      lbl.textContent = barHM(deep + rem);
      sc.svg.appendChild(lbl);
      if (n.restorative_pct != null && n.restorative_pct >= 33) {
        const check = svgEl("text", { x: sc.X(n.night), y: yTop - 16,
          "text-anchor": "middle", fill: cssVar("--good"), "font-size": 10 });
        check.textContent = "✓";
        sc.svg.appendChild(check);
      }
    }
    hitRect(sc, x - (sc.slot - bw) / 2, (ev) => healthTip(ev,
      `${fmtDur(deep + rem)} restorative` +
        (n.restorative_pct != null ? ` (${Math.round(n.restorative_pct)}%)` : ""),
      [`REM ${fmtDur(rem)} · Deep ${fmtDur(deep)}`, fmtNightLabel(n.night)]));
  }
  drawNightTicks(sc);
  container.appendChild(sc.svg);
}

// --- tonight's target: the score pipeline's target length, worked back to a
// bed time from the last week's typical wake time + awake overhead
function renderTarget() {
  const sl = healthData.sleep || {};
  const lenEl = document.getElementById("sd-target-len");
  const bedEl = document.getElementById("sd-target-bed");
  const wakeEl = document.getElementById("sd-target-wake");
  const note = document.getElementById("sd-target-note");
  lenEl.textContent = sl.target_sleep_min != null ? fmtDur(sl.target_sleep_min) : "—";
  const week = lastNights(healthHist, "7d").filter((n) => n.wake_ts != null);
  const wakeH = meanOf(week.map((n) => hSinceNoon(n.wake_ts, n.night)));
  if (sl.target_sleep_min == null || wakeH == null) {
    bedEl.textContent = "—"; wakeEl.textContent = "—";
    note.textContent = "Needs a few nights of history to place bed and wake times.";
    return;
  }
  const wasoAvg = meanOf(week.map((n) => n.waso_min).filter((v) => v != null)) || 0;
  const bedH = wakeH - (sl.target_sleep_min + wasoAvg) / 60;
  bedEl.textContent = `${noonHLabel(bedH)}*`;
  wakeEl.textContent = noonHLabel(wakeH);
  note.textContent = `* works back from your typical ${noonHLabel(wakeH)} wake over the ` +
    `last 7 nights and factors in your ${Math.round(wasoAvg)}m average time awake. ` +
    `Target length = your sleep need plus part of the current debt.`;
}

// --- sleep debt: the rolling 14-day shortfall, per night
function renderDebt() {
  const sl = healthData.sleep || {};
  const total = document.getElementById("sd-debt-total");
  if (sl.sleep_debt_min != null) {
    const hrs = sl.sleep_debt_min / 60;
    total.textContent = `${hrs > 0 ? "+" : ""}${hrs.toFixed(1)} h`;
    total.style.color = cssVar(hrs > 0 ? "--critical" : "--good");
  } else {
    total.textContent = "—"; total.style.color = "";
  }
  document.getElementById("sd-debt-note").textContent =
    `Shortfall vs your ${sleepNeedMin ? fmtDur(sleepNeedMin) : ""} nightly need over a ` +
    `rolling 14-day window — positive means you owe sleep, negative is banked surplus.`;
  const range = activeRange("sd-debt-range");
  drawDebtChart(document.getElementById("sd-debt-chart"), lastNights(healthHist, range), range);
}

function drawDebtChart(container, nights, rangeStr) {
  const data = nights.filter((n) => n.sleep_debt_min != null);
  container.textContent = "";
  if (data.length < 2) return chartEmpty(container, "Not enough scored nights in this range yet.");
  const sc = nightChart(container, rangeStr, { top: 10, right: 10, bottom: 18, left: 34 });
  const vals = data.map((n) => n.sleep_debt_min / 60);
  const yMin = Math.min(0, Math.floor(Math.min(...vals))) - 0.4;
  const yMax = Math.max(1, Math.ceil(Math.max(...vals))) + 0.4;
  const Y = (v) => sc.pad.top + sc.ih - (v - yMin) / (yMax - yMin) * sc.ih;
  const step = Math.max(1, Math.ceil((yMax - yMin) / 4));
  for (let g = Math.ceil(yMin); g <= yMax; g += step) {
    const y = Y(g);
    sc.svg.appendChild(svgEl("line", { x1: sc.pad.left, y1: y, x2: sc.w - sc.pad.right,
      y2: y, stroke: cssVar(g === 0 ? "--baseline" : "--grid"),
      "stroke-width": g === 0 ? 1.5 : 1 }));
    const t = svgEl("text", { x: sc.pad.left - 5, y: y + 3, fill: cssVar("--muted"),
      "font-size": 9, "text-anchor": "end",
      "font-family": "ui-monospace, Menlo, monospace" });
    t.textContent = g;
    sc.svg.appendChild(t);
  }
  sc.svg.appendChild(svgEl("path", {
    d: data.map((n, i) => `${i ? "L" : "M"}${sc.X(n.night).toFixed(1)},${Y(n.sleep_debt_min / 60).toFixed(1)}`).join(""),
    fill: "none", stroke: cssVar("--ink-2"), "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round" }));
  for (const n of data) {
    const v = n.sleep_debt_min / 60;
    sc.svg.appendChild(svgEl("circle", { cx: sc.X(n.night), cy: Y(v), r: 3.5,
      fill: cssVar(v > 0 ? "--critical" : "--good"),
      stroke: cssVar("--surface"), "stroke-width": 1.5 }));
    hitRect(sc, sc.X(n.night) - sc.slot / 2, (ev) => healthTip(ev,
      `${v > 0 ? "+" : ""}${v.toFixed(1)} h debt`, [fmtNightLabel(n.night)]));
  }
  drawNightTicks(sc);
  container.appendChild(sc.svg);
}

// --- consistency: one bar per night from bedtime down to wake, plus the SRI
function renderCons() {
  const sl = healthData.sleep || {};
  const sri = document.getElementById("sd-sri");
  if (sl.sri != null) {
    const v = Math.round(sl.sri);
    sri.textContent = `SRI ${v} — ${v >= 85 ? "excellent" : v >= 70 ? "good" : v >= 50 ? "fair" : "irregular"}`;
    sri.style.color = scoreColor(v);
  } else {
    sri.textContent = "SRI needs more nights"; sri.style.color = "";
  }
  const range = activeRange("sd-cons-range");
  const nights = lastNights(healthHist, range).filter(
    (n) => n.onset_ts != null && n.wake_ts != null);
  drawConsChart(document.getElementById("sd-cons-chart"), nights, range);
  const avgBox = document.getElementById("sd-cons-avg");
  avgBox.textContent = "";
  const onH = meanOf(nights.map((n) => hSinceNoon(n.onset_ts, n.night)));
  const wkH = meanOf(nights.map((n) => hSinceNoon(n.wake_ts, n.night)));
  if (onH != null) {
    for (const [label, h] of [["Average asleep", onH], ["Average wake", wkH]]) {
      const cell = document.createElement("div");
      const l = document.createElement("span");
      l.className = "sd-time-label"; l.textContent = label;
      const v = document.createElement("span");
      v.className = "sd-time"; v.textContent = noonHLabel(h);
      cell.append(l, v);
      avgBox.appendChild(cell);
    }
  }
}

function drawConsChart(container, nights, rangeStr) {
  container.textContent = "";
  if (!nights.length) return chartEmpty(container, "No bed/wake times in this range yet.");
  const labeled = parseInt(rangeStr, 10) <= 7;
  const sc = nightChart(container, rangeStr,
    { top: 16, right: 8, bottom: labeled ? 30 : 18, left: 44 });
  const hs = nights.map((n) => [hSinceNoon(n.onset_ts, n.night), hSinceNoon(n.wake_ts, n.night)]);
  const yLo = Math.floor(Math.min(...hs.map((p) => p[0]))) - 0.5;
  const yHi = Math.ceil(Math.max(...hs.map((p) => p[1]))) + 0.5;
  const Y = (h) => sc.pad.top + (h - yLo) / (yHi - yLo) * sc.ih; // earlier = higher
  const tickStep = yHi - yLo > 12 ? 3 : 2;
  for (let g = Math.ceil(yLo); g <= yHi; g += tickStep) {
    const y = Y(g);
    sc.svg.appendChild(svgEl("line", { x1: sc.pad.left, y1: y, x2: sc.w - sc.pad.right,
      y2: y, stroke: cssVar("--grid"), "stroke-width": 1 }));
    const t = svgEl("text", { x: sc.pad.left - 5, y: y + 3, fill: cssVar("--muted"),
      "font-size": 9, "text-anchor": "end",
      "font-family": "ui-monospace, Menlo, monospace" });
    t.textContent = noonHLabel(g);
    sc.svg.appendChild(t);
  }
  const bw = Math.min(sc.slot * 0.55, 20);
  nights.forEach((n, i) => {
    const [hOn, hWk] = hs[i];
    const x = sc.X(n.night);
    sc.svg.appendChild(svgEl("rect", { x: x - bw / 2, y: Y(hOn), width: bw,
      height: Math.max(2, Y(hWk) - Y(hOn)), rx: 3, fill: cssVar("--s-co2"), opacity: 0.9 }));
    if (labeled) {
      const top = svgEl("text", { x, y: Y(hOn) - 4, "text-anchor": "middle",
        fill: cssVar("--ink-2"), "font-size": 9,
        "font-family": "ui-monospace, Menlo, monospace" });
      top.textContent = noonHLabel(hOn);
      const bot = svgEl("text", { x, y: Y(hWk) + 11, "text-anchor": "middle",
        fill: cssVar("--ink-2"), "font-size": 9,
        "font-family": "ui-monospace, Menlo, monospace" });
      bot.textContent = noonHLabel(hWk);
      sc.svg.append(top, bot);
    }
    hitRect(sc, x - sc.slot / 2, (ev) => healthTip(ev,
      `${noonHLabel(hOn)} → ${noonHLabel(hWk)}`,
      [`${fmtDur((n.wake_ts - n.onset_ts) / 60)} in bed`, fmtNightLabel(n.night)]));
  });
  drawNightTicks(sc);
  container.appendChild(sc.svg);
}

/* ------------------------------------------------- vitals history dialog
   Opened from a vitals tile (that tile's metric shows first; the tabs switch
   between HRV / resting HR / respiration / SpO₂ / wrist temp). Nightly values
   over 7/30/60 days against the latest rolling-baseline band. */

let vitalsMetric = "hrv";

async function openVitalsDetail(metric) {
  vitalsMetric = metric in VITAL_DEFS ? metric : "hrv";
  syncVitalsTabs();
  vitalsOverlay.hidden = false;
  document.body.style.overflow = "hidden";
  document.getElementById("vitals-close").focus();
  try { await ensureHealthHist(); } catch { healthHist = []; }
  renderVitalsDialog();
}

function closeVitalsDetail() {
  vitalsOverlay.hidden = true;
  healthRestoreScroll();
}

function syncVitalsTabs() {
  document.querySelectorAll("#vitals-tabs .vtab").forEach((b) =>
    b.classList.toggle("active", b.dataset.vmetric === vitalsMetric));
}

function vitalsBand(def) {
  const b = healthData && healthData.baselines[def.base];
  if (!b || b.mean == null) return null;
  const conv = def.exp ? Math.exp : (x) => x;
  return { mid: conv(b.mean),
           lo: b.sd != null ? conv(b.mean - b.sd) : null,
           hi: b.sd != null ? conv(b.mean + b.sd) : null };
}

function renderVitalsDialog() {
  const def = VITAL_DEFS[vitalsMetric];
  document.getElementById("vitals-title").textContent = `${def.label} — nightly history`;
  const nights = healthHist || [];
  let recent = null;
  for (let i = nights.length - 1; i >= 0 && recent == null; i--) recent = nights[i][def.field];
  document.getElementById("vitals-recent").textContent =
    recent == null ? "—" : `${Number(recent).toFixed(def.digits)} ${def.unit}`;
  const band = vitalsBand(def);
  document.getElementById("vitals-baseline").textContent =
    band ? `${band.mid.toFixed(def.digits)} ${def.unit}` : "—";
  const range = activeRange("vitals-range");
  drawVitalsChart(document.getElementById("vitals-chart"),
                  lastNights(nights, range), range, def, band);
}

function drawVitalsChart(container, nights, rangeStr, def, band) {
  const data = nights.filter((n) => n[def.field] != null);
  container.textContent = "";
  if (data.length < 2) return chartEmpty(container, "Not enough nights in this range yet.");
  const sc = nightChart(container, rangeStr, { top: 16, right: 10, bottom: 18, left: 40 });
  const vals = data.map((n) => n[def.field]);
  let yMin = Math.min(...vals), yMax = Math.max(...vals);
  if (band && band.lo != null) { yMin = Math.min(yMin, band.lo); yMax = Math.max(yMax, band.hi); }
  const spread = (yMax - yMin) || 1;
  yMin -= spread * 0.15; yMax += spread * 0.15;
  const Y = (v) => sc.pad.top + sc.ih - (v - yMin) / (yMax - yMin) * sc.ih;
  if (band && band.lo != null) {
    sc.svg.appendChild(svgEl("rect", { x: sc.pad.left, y: Y(band.hi),
      width: sc.iw, height: Y(band.lo) - Y(band.hi),
      fill: cssVar("--baseline"), opacity: 0.35 }));
    sc.svg.appendChild(svgEl("line", { x1: sc.pad.left, y1: Y(band.mid),
      x2: sc.w - sc.pad.right, y2: Y(band.mid), stroke: cssVar("--muted"),
      "stroke-width": 1, "stroke-dasharray": "2 4", opacity: 0.8 }));
  }
  const decimals = def.digits;
  for (const frac of [0, 0.5, 1]) {
    const v = yMin + (yMax - yMin) * frac;
    const y = Y(v);
    sc.svg.appendChild(svgEl("line", { x1: sc.pad.left, y1: y, x2: sc.w - sc.pad.right,
      y2: y, stroke: cssVar("--grid"), "stroke-width": 1 }));
    const t = svgEl("text", { x: sc.pad.left - 5, y: y + 3, fill: cssVar("--muted"),
      "font-size": 9, "text-anchor": "end",
      "font-family": "ui-monospace, Menlo, monospace" });
    t.textContent = v.toFixed(decimals);
    sc.svg.appendChild(t);
  }
  sc.svg.appendChild(svgEl("path", {
    d: data.map((n, i) => `${i ? "L" : "M"}${sc.X(n.night).toFixed(1)},${Y(n[def.field]).toFixed(1)}`).join(""),
    fill: "none", stroke: cssVar("--copper"), "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round" }));
  data.forEach((n, i) => {
    const last = i === data.length - 1;
    sc.svg.appendChild(svgEl("circle", { cx: sc.X(n.night), cy: Y(n[def.field]),
      r: last ? 4 : 3, fill: cssVar("--copper"),
      stroke: cssVar("--surface"), "stroke-width": last ? 2 : 1.5 }));
    if (data.length <= 10) {
      const lbl = svgEl("text", { x: sc.X(n.night), y: Y(n[def.field]) - 8,
        "text-anchor": "middle", fill: cssVar("--ink-2"), "font-size": 9,
        "font-family": "ui-monospace, Menlo, monospace" });
      lbl.textContent = Number(n[def.field]).toFixed(decimals);
      sc.svg.appendChild(lbl);
    }
    hitRect(sc, sc.X(n.night) - sc.slot / 2, (ev) => healthTip(ev,
      `${Number(n[def.field]).toFixed(decimals)} ${def.unit}`,
      band ? [fmtNightLabel(n.night), `baseline ${band.mid.toFixed(decimals)} ${def.unit}`]
           : [fmtNightLabel(n.night)]));
  });
  drawNightTicks(sc);
  container.appendChild(sc.svg);
}

// --- dialog wiring: card/tile entry points, tabs, range groups, close paths
function activeRange(groupId) {
  return document.getElementById(groupId).querySelector(".range-btn.active").dataset.range;
}
function wireRange(groupId, onChange) {
  const group = document.getElementById(groupId);
  group.querySelectorAll(".range-btn").forEach((btn) => btn.addEventListener("click", () => {
    if (btn.classList.contains("active")) return;
    group.querySelectorAll(".range-btn").forEach((b) =>
      b.classList.toggle("active", b === btn));
    onChange();
  }));
}
wireRange("sd-resto-range", renderResto);
wireRange("sd-debt-range", renderDebt);
wireRange("sd-cons-range", renderCons);
wireRange("vitals-range", renderVitalsDialog);

document.querySelectorAll("#vitals-tabs .vtab").forEach((btn) =>
  btn.addEventListener("click", () => {
    if (btn.dataset.vmetric === vitalsMetric) return;
    vitalsMetric = btn.dataset.vmetric;
    syncVitalsTabs();
    renderVitalsDialog();
  }));

const sleepCard = document.getElementById("sleep-card");
sleepCard.addEventListener("click", openSleepDetail);
sleepCard.addEventListener("keydown", (ev) => {
  if ((ev.key === "Enter" || ev.key === " ") && ev.target === sleepCard) {
    ev.preventDefault();
    openSleepDetail();
  }
});
document.getElementById("sleepd-close").addEventListener("click", closeSleepDetail);
document.getElementById("sleepd-backdrop").addEventListener("click", closeSleepDetail);
document.getElementById("vitals-close").addEventListener("click", closeVitalsDetail);
document.getElementById("vitals-backdrop").addEventListener("click", closeVitalsDetail);

document.querySelectorAll(
  ".widget[data-widget='metric'], .widget[data-widget='motion'], .widget[data-widget='nights']")
  .forEach(wireWidget);

(async () => {
  await loadThresholds();
  await pollFast();     // also sets activeScene, which the summary card needs
  await loadSummary();
  await loadAwaySummary();
  await loadNights();
  refreshAllSparks();
  // deep-link: /#temp, /#co2, /#motion, /#power-1 (device id) opens that
  // detail; an optional range suffix like /#temp:3h preselects the range;
  // /#settings opens the threshold dialog; /#planner and /#health open
  // those views
  const [hash, hashRange] = location.hash.slice(1).split(":");
  if (hash === "planner" || hash === "health") {
    showView(hash);
  } else if (hash === "settings") {
    openSettings();
  } else if (hash) {
    const powerMatch = hash.match(/^power-(\d+)$/);
    const widget = powerMatch
      ? document.querySelector(`.power-widget[data-device-id="${powerMatch[1]}"]`)
      : document.querySelector(`.widget[data-metric="${hash}"], .widget[data-widget="${hash}"]`);
    if (widget) {
      if (hashRange) {
        widget.dataset.range = hashRange;
        widget._detail.querySelectorAll(".detail-range .range-btn").forEach((b) =>
          b.classList.toggle("active", b.dataset.range === hashRange));
      }
      openDetail(widget);
    }
  }
  setInterval(pollFast, POLL_FAST_MS);
  setInterval(() => {
    refreshAllSparks();
    if (openWidget) loadDetail(openWidget);
    // keeps the calendar's today marker and now-line honest, and picks up
    // edits made from another device — but never rebuilds the grid under an
    // open event dialog or an in-flight drag
    if (!plannerView.hidden && eventOverlay.hidden && !calDragging) refreshPlanner();
  }, POLL_SLOW_MS);
})();
