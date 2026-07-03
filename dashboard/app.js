/* HUB-01 dashboard — vanilla JS, no build step.
   One-page widget board. Live values poll every 5s; sparklines and an open
   detail dialog refresh every 30s. Clicking a widget opens its detail in an
   overlay dialog (nothing on the board moves); the switch cards control the
   plugs and lighting zones. Metric details overlay a "typical day" curve
   (7-day average per half-hour of the day) on the 24h chart. */

"use strict";

const POLL_FAST_MS = 5000;   // latest values, plug states, ticker
const POLL_SLOW_MS = 30000;  // sparklines + open detail dialog
const SPARK_RANGE = "3h";

const METRICS = [
  { id: "temp", key: "TEMP", unit: "°C", decimals: 1, color: cssVar("--s-temp") },
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

async function postJSON(url, body) {
  const opts = { method: "POST" };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `${url} -> ${resp.status}`);
  return data;
}

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

function renderPIR(latest) {
  const r = latest.motion;
  const stateEl = document.getElementById("pir-state");
  const label = document.getElementById("pir-label");
  if (!r) { label.textContent = "no data"; return; }
  const active = Number(r.value) === 1;
  stateEl.classList.toggle("active", active);
  label.textContent = active ? "MOTION" : "quiet";
  document.getElementById("pir-note").textContent = active
    ? "PIR is reporting movement right now."
    : `Last report ${relTime(r.ts)}. HC-SR501 on pin D2.`;
}

async function pollFast() {
  try {
    const [latest, devices] = await Promise.all([
      getJSON("/api/sensors/latest"),
      getJSON("/api/devices"),
    ]);
    setLink(true);
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
      <header class="card-head">
        <code class="wire-key"></code>
        <button class="plug-lock-btn" type="button" aria-pressed="false"
                title="Lock to prevent power-off" disabled>
          <svg class="lock-icon-unlocked" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 7.75-1.5"></path></svg>
          <svg class="lock-icon-locked" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 8 0v4"></path></svg>
        </button>
      </header>
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
          <button class="range-btn" data-range="3h">3h</button>
          <button class="range-btn active" data-range="24h">24h</button>
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
   One .light-card per wled_zone device. The top-right switch is always the
   zone's physical on/off; Mode (manual/auto) lives in the control rows with
   brightness/color/effect. In 'auto' mode the lighting job (app/lighting.py)
   drives on/brightness from lux, so those two controls go read-only and
   just keep reflecting the live value each poll — they aren't hidden, since
   color/effect stay user-editable in either mode. */

const lightCards = new Map(); // device id -> .light-card element
const WARMTH_DEFAULT_K = 2700; // typical warm-white home bulb

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex([r, g, b]) {
  return "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
}

// Approximate black-body RGB for a color temperature in Kelvin (Tanner
// Helland's fit) — good enough for a pleasant warm/cool slider, not colorimetry.
function kelvinToRgb(kelvin) {
  const temp = kelvin / 100;
  let r, g, b;
  if (temp <= 66) {
    r = 255;
    g = temp <= 19 ? 0 : 99.4708025861 * Math.log(temp) - 161.1195681661;
  } else {
    r = 329.698727446 * Math.pow(temp - 60, -0.1332047592);
    g = 288.1221695283 * Math.pow(temp - 60, -0.0755148492);
  }
  b = temp >= 66 ? 255 : temp <= 19 ? 0 : 138.5177312231 * Math.log(temp - 10) - 305.0447927307;
  return [r, g, b].map((v) => Math.max(0, Math.min(255, Math.round(v))));
}

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
        <input type="range" min="2000" max="6500" step="50" value="${WARMTH_DEFAULT_K}" class="light-warmth" disabled>
        <span class="light-warmth-value">${WARMTH_DEFAULT_K}K</span>
      </div>
      <label class="light-field light-rgb-field" hidden>
        <span class="light-field-label"></span>
        <input type="color" class="light-color" disabled>
      </label>
      <label class="light-field">
        <span class="light-field-label">Effect</span>
        <select class="light-effect" disabled>
          <option value="0">Solid</option>
          <option value="1">Blink</option>
          <option value="2">Breathe</option>
          <option value="3">Wipe</option>
          <option value="8">Colorloop</option>
          <option value="9">Rainbow</option>
        </select>
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
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      modeButtons.forEach((b) => b.classList.toggle("active", b === btn));
      const isAmbient = btn.dataset.mode === "ambient";
      card.querySelector(".light-warmth-field").hidden = !isAmbient;
      card.querySelector(".light-rgb-field").hidden = isAmbient;
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
      applyLightState(card, await postJSON(`/api/devices/${device.id}/state`,
        { color: kelvinToRgb(Number(warmth.value)) }));
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

  const effect = card.querySelector(".light-effect");
  effect.addEventListener("change", async () => {
    try {
      applyLightState(card, await postJSON(`/api/devices/${device.id}/state`,
        { effect: Number(effect.value) }));
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
    card.querySelector(".light-color-swatch").style.background = rgbToHex(state.color);
  }
  if (state.effect !== undefined) card.querySelector(".light-effect").value = String(state.effect);
}

function renderLighting(devices, latest) {
  const list = document.getElementById("lighting-list");
  const empty = document.getElementById("lighting-empty");
  const lux = latest && latest.lux;
  for (const device of devices) {
    if (device.type !== "wled_zone") continue;
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
    const effect = card.querySelector(".light-effect");

    if (device.light) {
      // brightness (and on/off) keep reflecting the live value every poll,
      // whether a manual edit or the auto job's last tick set it
      applyLightState(card, device.light);
      color.disabled = false;
      warmth.disabled = false;
      colorModeBtns.forEach((b) => { b.disabled = false; });
      effect.disabled = false;
      status.textContent = isAuto
        ? `Auto — following ambient light${lux ? ` (${Number(lux.value).toFixed(0)} lx)` : ""}.`
        : "";
    } else {
      color.disabled = true;
      warmth.disabled = true;
      colorModeBtns.forEach((b) => { b.disabled = true; });
      effect.disabled = true;
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
  else if (!settingsOverlay.hidden) closeSettings();
  else if (!overlayEl.hidden) closeDetail();
});

/* -------------------------------------------------------- settings dialog */

const settingsOverlay = document.getElementById("settings-overlay");
const settingsForm = document.getElementById("settings-form");
const saveNote = document.getElementById("save-note");

function openSettings() {
  if (thresholds) {
    settingsForm.querySelectorAll("input").forEach((input) => {
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
}

function closeSettings() {
  settingsOverlay.hidden = true;
  if (overlayEl.hidden && unlockOverlay.hidden) document.body.style.overflow = "";
  document.getElementById("settings-btn").focus();
}

document.getElementById("settings-btn").addEventListener("click", openSettings);
document.getElementById("settings-close").addEventListener("click", closeSettings);
document.getElementById("settings-backdrop").addEventListener("click", closeSettings);

settingsForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = {};
  settingsForm.querySelectorAll("input").forEach((input) => {
    const key = input.dataset.key;
    body[key] = body[key] || {};
    body[key][input.dataset.bound] =
      input.value.trim() === "" ? null : Number(input.value);
  });
  try {
    const resp = await fetch("/api/settings/thresholds", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || "save failed");
    thresholds = data;
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
  if (overlayEl.hidden && settingsOverlay.hidden) document.body.style.overflow = "";
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
  const range = widget.dataset.range || "24h";
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
          thresholds: thresholds ? thresholds.power : null });
      setStat(widget, "avg_24h_w", stats.avg_24h_w, "W");
      setStat(widget, "kwh_24h", stats.kwh_24h, "kWh");
      setStat(widget, "avg_7d_w", stats.avg_7d_w, "W");
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

  svg.addEventListener("pointermove", (ev) => {
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

document.querySelectorAll(".widget[data-widget='metric'], .widget[data-widget='motion']")
  .forEach(wireWidget);

(async () => {
  await loadThresholds();
  await pollFast();
  refreshAllSparks();
  // deep-link: /#temp, /#co2, /#motion, /#power-1 (device id) opens that
  // detail; an optional range suffix like /#temp:3h preselects the range;
  // /#settings opens the threshold dialog
  const [hash, hashRange] = location.hash.slice(1).split(":");
  if (hash === "settings") {
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
  }, POLL_SLOW_MS);
})();
