// brick-console dashboard — DOM glue (R2; architecture §8: vanilla JS).
//
// Thin by design: this module owns the DOM and the WebSocket, and nothing
// else. Every value → view-model decision lives in dashboard.mjs (pure,
// unit-tested under node --test); this file renders what that module
// decides: apply the envelope → render the state. The stale sweep runs on
// a 250 ms interval (half the shortest STALE_MS entry) so dimming engages
// within one cadence period of a feed cut.
//
// No framework, no bundler, no CDN (the LAN box may be offline) — plain
// ES modules served by the static mount.

import {
  applyEnvelope,
  chipView,
  initialDashboard,
  isDimmed,
  isMockMode,
  nextReconnectDelay,
  staleness,
  STALE_MS,
  wsUrl,
} from "./dashboard.mjs";

const staleSweepMs = 250; // half the shortest stale timeout (500 ms)

const chipEl = document.getElementById("chip");
const reasonEl = document.getElementById("reason");
const dimEl = document.getElementById("dashboard");
const hubNameEl = document.getElementById("hub-name");
const hubFwEl = document.getElementById("hub-firmware");
const hubModelEl = document.getElementById("hub-model");
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
    <ul class="port-values"></ul>`;
  portsEl.appendChild(card);
  portEls.set(letter, {
    card,
    device: card.querySelector(".port-device"),
    values: card.querySelector(".port-values"),
  });
}

let dash = initialDashboard();
let logRendered = 0; // <li> nodes currently in the pane (render delta)
let socket = null;
let reconnectAttempt = 0;
let reconnectTimer = null;

const now = () => Date.now();

// -- Rendering --------------------------------------------------------------
//
// Each render is a full state pass: six ports and a handful of scalars —
// innerHTML for the value rows, textContent for the scalars, no keyed
// diffing needed at this size.

function render() {
  const t = now();

  const chip = chipView(dash);
  chipEl.textContent = chip.label;
  chipEl.dataset.tone = chip.tone;
  reasonEl.textContent = dash.external
    ? `external client took the hub — the server backs off and rescans · ${dash.reason}`
    : dash.reason;

  // Dimming: OFFLINE (incl. EXTERNAL overlay) grays the cards out with
  // "last seen" timestamps (the state envelope drives all cards).
  dimEl.classList.toggle("dimmed", isDimmed(dash));

  paneStale();
}

function paneStale() {
  const t = now();

  hubNameEl.textContent = dash.hub.name ?? "—";
  hubFwEl.textContent = dash.hub.firmware ?? "—";
  hubModelEl.textContent = dash.hub.model ?? "—";

  const batteryStale = staleness(dash.battery.at, t, STALE_MS.battery);
  batteryPctEl.textContent =
    dash.battery.percent === null ? "—" : `${dash.battery.percent}%`;
  batteryMvEl.textContent =
    dash.battery.voltageMv === null ? "—" : `${dash.battery.voltageMv} mV`;
  batteryMaEl.textContent =
    dash.battery.currentMa === null ? "—" : `${dash.battery.currentMa} mA`;
  batterySeenEl.textContent = seenText(dash.battery.at, batteryStale, t);
  batteryCardEl.classList.toggle("stale", batteryStale.stale);

  // IMU widget — accel/gyro numbers + the `up` side-string badge (M1's
  // minimal orientation cue; no 3D).
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
  imuSeenEl.textContent = seenText(dash.imu.at, imuStale, t);
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
  const { card, device, values } = portEls.get(letter);
  const port = dash.ports[letter] ?? { device: null, rows: [], at: null };
  const portStale = staleness(port.at, t, STALE_MS.port);

  if (port.device === null) {
    // No port envelope ever seen: render the dimmed empty port.
    card.classList.add("empty");
    card.classList.remove("stale");
    device.textContent = "";
    values.innerHTML = "";
    return;
  }
  card.classList.toggle("empty", port.device === "none");
  card.classList.toggle("stale", portStale.stale);
  device.textContent = port.device;
  values.innerHTML = port.rows
    .map((row) => `<li><span class="k">${escapeHtml(row.key)}</span> ${escapeHtml(row.text)}</li>`)
    .join("");
}

function seenText(at, stale, t) {
  if (at === null) return "never seen";
  return `${stale.stale ? "stale" : "live"} · last seen ${formatAge(t - at)}`;
}

function formatAge(ms) {
  const s = Math.max(0, ms) / 1000;
  if (s < 10) return `${s.toFixed(1)}s ago`;
  if (s < 60) return `${Math.floor(s)}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

function escapeHtml(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function renderLog() {
  const lines = dash.logs;
  if (lines.length < logRendered) {
    rebuildLog(lines);
    return;
  }
  for (let i = logRendered; i < lines.length; i++) {
    const li = document.createElement("li");
    li.textContent = lines[i];
    logListEl.appendChild(li);
  }
  logRendered = lines.length;
  logPaneEl.scrollTop = logPaneEl.scrollHeight;
}

function rebuildLog(lines) {
  logListEl.replaceChildren(
    ...lines.map((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      return li;
    }),
  );
  logRendered = lines.length;
  logPaneEl.scrollTop = logPaneEl.scrollHeight;
}

// -- WebSocket ---------------------------------------------------------------

function connect() {
  clearTimeout(reconnectTimer);
  socket = new WebSocket(wsUrl(location, isMockMode(location.search)));

  socket.onmessage = (event) => {
    let envelope;
    try {
      envelope = JSON.parse(event.data);
    } catch {
      return;
    }
    dash = applyEnvelope(dash, envelope, now());
    render();
  };

  socket.onopen = () => {
    reconnectAttempt = 0;
    render();
  };

  socket.onclose = () => {
    const delay = nextReconnectDelay(reconnectAttempt);
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(connect, delay);
  };

  socket.onerror = () => socket.close();
}

// -- Boot --------------------------------------------------------------------

render();
connect();

setInterval(render, staleSweepMs);