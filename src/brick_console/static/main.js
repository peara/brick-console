// brick-console dashboard — DOM glue (R2; architecture §8: vanilla JS).
//
// Thin by design: this module owns the DOM and the WebSocket, and nothing
// else. Every value → view-model decision lives in dashboard.mjs (pure,
// unit-tested under node --test); this file renders what that module
// decides. Envelope arrival only updates the view model (latest-wins,
// receipt-time stamped) — painting is coalesced to the PAINT_MS tick,
// which also carries the stale sweep (R5): the wire can burst (the join
// replay ships the whole ring at once; mock runs ~60 envelopes/s
// steady), but the DOM sees at most PAINT_MS-paced full passes, so the
// numbers stay readable and the page stays interactive.
//
// No framework, no bundler, no CDN (the LAN box may be offline) — plain
// ES modules served by the static mount.

import {
  applyEnvelope,
  chipView,
  formatAge,
  initialDashboard,
  isDimmed,
  isMockMode,
  logSyncPlan,
  nextReconnectDelay,
  PAINT_MS,
  seenStamp,
  seenText,
  STALE_MS,
  wsUrl,
} from "./dashboard.mjs";

const followThresholdPx = 24; // "user is at the bottom" band for log follow

const chipEl = document.getElementById("chip");
const reasonEl = document.getElementById("reason");
const dimEl = document.getElementById("dashboard");
const hubNameEl = document.getElementById("hub-name");
const hubFwEl = document.getElementById("hub-firmware");
const hubModelEl = document.getElementById("hub-model");
const hubSeenEl = document.getElementById("hub-seen");
const batteryCardEl = document.getElementById("battery-card");
const batteryPctEl = document.getElementById("battery-pct");
const batteryMvEl = document.getElementById("battery-mv");
const batteryMaEl = document.getElementById("battery-ma");
const batterySeenEl = document.getElementById("battery-seen");
const imuCardEl = document.getElementById("imu-card");
const imuAccelEl = document.getElementById("imu-accel");
const imuGyroEl = document.getElementById("imu-gyro");
const imuUpEl = document.getElementById("imu-up");
const imuSeenEl = document.getElementById("imu-seen");
const portsEl = document.getElementById("ports");
const logPaneEl = document.getElementById("log-pane");
const logListEl = document.getElementById("log-lines");
const unknownEl = document.getElementById("unknown-kinds");

const PORT_LETTERS = ["A", "B", "C", "D", "E", "F"];

// Build the six port chips once; render only updates their content.
const portEls = new Map();
for (const letter of PORT_LETTERS) {
  const card = document.createElement("section");
  card.className = "port-card";
  card.innerHTML = `
    <header><span class="port-letter">${letter}</span><span class="port-device"></span></header>
    <ul class="port-values"></ul>
    <p class="seen port-seen"></p>`;
  portsEl.appendChild(card);
  portEls.set(letter, {
    card,
    device: card.querySelector(".port-device"),
    values: card.querySelector(".port-values"),
    seen: card.querySelector(".port-seen"),
  });
}

let dash = initialDashboard();
let renderedLogSeq = 0; // log envelopes already in the DOM (syncs via logSeq)
let socket = null;
let reconnectAttempt = 0;
let reconnectTimer = null;

const now = () => Date.now();

function render() {
  const chip = chipView(dash);
  chipEl.textContent = chip.label;
  chipEl.dataset.tone = chip.tone;
  reasonEl.textContent = dash.external
    ? `external client took the hub — the server backs off and rescans · ${dash.reason}`
    : dash.reason;
  dimEl.classList.toggle("dimmed", isDimmed(dash));
  renderPanes();
}

function renderPanes() {
  const t = now();

  hubNameEl.textContent = dash.hub.name ?? "—";
  hubFwEl.textContent = dash.hub.firmware ?? "—";
  hubModelEl.textContent = dash.hub.model ?? "—";
  hubSeenEl.textContent = seenStamp(dash.hub.at, t);

  const batteryStale = staleness(dash.battery.at, t, STALE_MS.battery);
  batteryPctEl.textContent =
    dash.battery.percent === null ? "—" : `${dash.battery.percent}%`;
  batteryMvEl.textContent =
    dash.battery.voltageMv === null ? "—" : `${dash.battery.voltageMv} mV`;
  batteryMaEl.textContent =
    dash.battery.currentMa === null ? "—" : `${dash.battery.currentMa} mA`;
  batterySeenEl.textContent = seenText(dash.battery.at, t, STALE_MS.battery);
  batteryCardEl.classList.toggle("stale", batteryStale.stale);

  const imuStale = staleness(dash.imu.at, t, STALE_MS.imu);
  imuAccelEl.textContent =
    dash.imu.ax === null
      ? "—"
      : `${dash.imu.ax} / ${dash.imu.ay} / ${dash.imu.az} mm/s²`;
  imuGyroEl.textContent =
    dash.imu.gx === null
      ? "—"
      : `${dash.imu.gx} / ${dash.imu.gy} / ${dash.imu.gz} °/s`;
  imuUpEl.textContent = dash.imu.up ?? "—";
  imuSeenEl.textContent = seenText(dash.imu.at, t, STALE_MS.imu);
  imuCardEl.classList.toggle("stale", imuStale.stale);

  for (const letter of PORT_LETTERS) {
    renderPort(letter, t);
  }

  const n = dash.unknown.count;
  unknownEl.textContent =
    n === 0
      ? ""
      : `${n} unknown telemetry kind${n === 1 ? "" : "s"}` +
        (dash.unknown.last ? ` (last: ${dash.unknown.last})` : "");

  renderLog();
}

function renderPort(letter, t) {
  const { card, device, values, seen } = portEls.get(letter);
  const port = dash.ports[letter] ?? { device: null, rows: [], at: null };
  const portStale = staleness(port.at, t, STALE_MS.port);

  if (port.device === null) {
    card.classList.add("empty");
    card.classList.remove("stale");
    device.textContent = "";
    values.innerHTML = "";
    seen.textContent = "";
    return;
  }
  card.classList.toggle("empty", port.device === "none");
  card.classList.toggle("stale", portStale.stale);
  device.textContent = port.device;
  seen.textContent = port.device === "none" ? "" : seenText(port.at, t, STALE_MS.port);
  values.innerHTML = port.rows
    .map((row) => `<li><span class="k">${escapeHtml(row.key)}</span> ${escapeHtml(row.text)}</li>`)
    .join("");
}

function staleness(at, t, timeoutMs) {
  if (at === null) return { stale: true, ageMs: null };
  const ageMs = Math.max(0, t - at);
  return { stale: ageMs >= timeoutMs, ageMs };
}

function escapeHtml(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function renderLog() {
  const plan = logSyncPlan(dash.logs, dash.logSeq, renderedLogSeq);
  if (plan.kind === "none") return;

  // Follow only when the user is at (or near) the bottom, so reading the
  // scrollback isn't yanked away by the next line (the pane keeps the
  // newest LOG_SCROLLBACK lines; scrolling up pins the view).
  const follow =
    logPaneEl.scrollHeight - logPaneEl.scrollTop - logPaneEl.clientHeight <
    followThresholdPx;

  if (plan.kind === "rebuild") {
    logListEl.replaceChildren(
      ...plan.lines.map((line) => {
        const li = document.createElement("li");
        li.textContent = line;
        return li;
      }),
    );
  } else {
    for (const line of plan.lines) {
      const li = document.createElement("li");
      li.textContent = line;
      logListEl.appendChild(li);
    }
    // The ring may have dropped older lines from its front: trim the DOM
    // in step so the pane mirrors the ring exactly (newest N lines).
    while (logListEl.childElementCount > dash.logs.length) {
      logListEl.firstElementChild.remove();
    }
  }
  renderedLogSeq = dash.logSeq;
  if (follow) logPaneEl.scrollTop = logPaneEl.scrollHeight;
}

// -- WebSocket ---------------------------------------------------------------

function connect() {
  clearTimeout(reconnectTimer);
  const sock = new WebSocket(wsUrl(location, isMockMode(location.search)));
  socket = sock;

  sock.onmessage = (event) => {
    if (sock !== socket) return; // superseded mid-flight: ignore
    let envelope;
    try {
      envelope = JSON.parse(event.data);
    } catch {
      return;
    }
    // View model only — painting happens on the PAINT_MS tick.
    dash = applyEnvelope(dash, envelope, now());
  };

  sock.onopen = () => {
    if (sock !== socket) return;
    reconnectAttempt = 0;
  };

  sock.onclose = () => {
    if (sock !== socket) return; // a stale socket's close must not double-schedule
    const delay = nextReconnectDelay(reconnectAttempt);
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(connect, delay);
  };

  sock.onerror = () => sock.close();
}

// -- Boot --------------------------------------------------------------------

render();
connect();

// The paint clock: coalesces every envelope that arrived since the last
// tick into one full pass, and carries the stale sweep (R5) — staleness
// is recomputed from receipt stamps at paint time, so a paint shows each
// value's latest snapshot plus its current live/stale verdict.
setInterval(render, PAINT_MS);