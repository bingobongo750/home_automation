/* HUB-01 dashboard — vanilla JS, no build step.
   One-page widget board. Live values poll every 5s; sparklines and an open
   detail dialog refresh every 30s. Clicking a widget opens its detail in an
   overlay dialog (nothing on the board moves); the switch cards control the
   plugs. Metric details overlay a "typical day" curve (7-day average per
   half-hour of the day) on the 24h chart. */

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

async function postJSON(url) {
  const resp = await fetch(url, { method: "POST" });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || `${url} -> ${resp.status}`);
  return body;
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
        <h3 class="card-label"></h3><code class="wire-key"></code>
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
  pair.querySelector(".plug-control .card-label").textContent = device.name;
  pair.querySelector(".plug-control .wire-key").textContent = device.ip || "no ip";
  pair.querySelector(".plug-room").textContent = device.room || "";

  const widget = pair.querySelector(".power-widget");
  widget.dataset.deviceId = device.id;
  widget.dataset.name = device.name;
  wireWidget(widget);

  const sw = pair.querySelector(".plug-switch");
  sw.addEventListener("click", async () => {
    if (sw.disabled) return;
    sw.disabled = true;
    const note = pair.querySelector(".card-note");
    try {
      const result = await postJSON(`/api/devices/${device.id}/toggle`);
      applySwitch(pair, Boolean(result.relay_on));
      note.textContent = result.relay_on
        ? "Turned on. Draw updates on the next poll."
        : "Turned off.";
    } catch {
      note.textContent = "Couldn't reach the plug — is it powered and on the network?";
    } finally {
      sw.disabled = false;
    }
  });
  return pair;
}

function applySwitch(pair, on) {
  const sw = pair.querySelector(".plug-switch");
  sw.setAttribute("aria-checked", String(on));
  pair.querySelector(".switch-label").textContent = on ? "On" : "Off";
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
    const power = device.power;
    if (!power) continue;
    applySwitch(pair, power.relay_on === 1);
    pair.querySelector(".plug-switch").disabled = false;
    pair.querySelector(".power-widget .value").textContent =
      power.watts === null ? "—" : Number(power.watts).toFixed(1);
    pair.querySelector(".card-note").textContent = `Polled ${relTime(power.ts)}.`;
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
  if (ev.key === "Escape" && !overlayEl.hidden) closeDetail();
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
      const [history, stats, profile] = await Promise.all([
        getJSON(`/api/sensors/history?metric=${m.id}&range=${range}`),
        getJSON(`/api/sensors/stats?metric=${m.id}`),
        getJSON(`/api/sensors/profile?metric=${m.id}`),
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

      // On the 24h view, overlay the typical-day curve mapped onto the
      // rolling window (each time-of-day occurs exactly once in 24h).
      let overlaySeries = null;
      if (range === "24h" && profile.points.length >= 2) {
        const now = Date.now() / 1000;
        overlaySeries = {
          label: "Typical day (7d avg)",
          points: profile.points
            .map((p) => ({ ts: now - ((nowTod - p.tod + 86400) % 86400), value: p.value }))
            .sort((a, b) => a.ts - b.ts),
        };
      }
      renderLegend(detail, m.color, overlaySeries);
      drawChart(detail.querySelector(".chart"), history.points,
        { ...m, overlay: overlaySeries });
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
        { unit: "W", decimals: 1, color: POWER_COLOR });
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

function renderLegend(detail, color, overlaySeries) {
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
  for (const [label, opacity] of [["Last 24h", 1], [overlaySeries.label, 0.45]]) {
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
  el("path", { d: `${lineD}L${X(xMax).toFixed(1)},${Y(yMin).toFixed(1)}L${X(xMin).toFixed(1)},${Y(yMin).toFixed(1)}Z`,
               fill: opts.color, opacity: 0.1 });
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
  await pollFast();
  refreshAllSparks();
  // deep-link: /#temp, /#co2, /#motion, /#power-1 (device id) opens that detail
  const hash = location.hash.slice(1);
  if (hash) {
    const powerMatch = hash.match(/^power-(\d+)$/);
    const widget = powerMatch
      ? document.querySelector(`.power-widget[data-device-id="${powerMatch[1]}"]`)
      : document.querySelector(`.widget[data-metric="${hash}"], .widget[data-widget="${hash}"]`);
    if (widget) openDetail(widget);
  }
  setInterval(pollFast, POLL_FAST_MS);
  setInterval(() => {
    refreshAllSparks();
    if (openWidget) loadDetail(openWidget);
  }, POLL_SLOW_MS);
})();
