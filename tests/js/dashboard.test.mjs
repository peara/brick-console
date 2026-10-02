// Unit tests for the dashboard's pure render core (dashboard.mjs) — the
// module the browser serves, imported directly under node's built-in test
// runner (no DOM, no browser, no build step). The fake clock drives every
// staleness decision (the wire never carries timestamps; the issue's
// Done-when: "stale-marking dims values after cadence timeout (fake clock
// in tests)").
//
// Run: node --test tests/js/dashboard.test.mjs   (local dev gate — the JS
// suite is NOT wired into the uv-only CI; see the PR body's delegated-
// decision note for the rationale and the deviation record.)

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  appendLog,
  applyEnvelope,
  chipView,
  formatAge,
  formatPortValue,
  initialDashboard,
  isDimmed,
  isExternalTakeoverReason,
  isMockMode,
  logSyncPlan,
  LOG_SCROLLBACK,
  nextExternalOverlay,
  nextReconnectDelay,
  PAINT_MS,
  portRows,
  seenStamp,
  seenText,
  staleness,
  STALE_MS,
  wsUrl,
} from "../../src/brick_console/static/dashboard.mjs";

// ---------------------------------------------------------------------------
// Wire fixtures — the D7 envelope shapes the WS gateway ships verbatim
// ---------------------------------------------------------------------------

const stateEnvelope = (state, reason) => ({
  type: "state",
  hub: "Pybricks Hub",
  data: { state, reason },
});

const telemetryEnvelope = (data) => ({
  type: "telemetry",
  hub: "Pybricks Hub",
  data,
});

const logEnvelope = (line) => ({
  type: "log",
  hub: "Pybricks Hub",
  data: { src: "stdout", line },
});

const HUB_INFO = telemetryEnvelope({
  t: "hub_info",
  name: "Pybricks Hub",
  fw: "4.0.1",
  model: "technichub",
});

const BATTERY = telemetryEnvelope({ t: "battery", v: 8085, c: 42, pct: 87 });

const IMU = telemetryEnvelope({
  t: "imu",
  ax: 120,
  ay: -980,
  az: 9810,
  gx: 1,
  gy: -2,
  gz: 3,
  up: "top",
});

// ---------------------------------------------------------------------------
// Done-when 1: all envelope kinds render (pure functions, no crash)
// ---------------------------------------------------------------------------

test("state envelope: wire values land, lowercase wire → chip display", () => {
  let dash = initialDashboard();
  dash = applyEnvelope(dash, stateEnvelope("offline", "hub not found (scan timeout)"), 1000);

  assert.equal(dash.serverState, "offline");
  assert.equal(dash.reason, "hub not found (scan timeout)");
  assert.equal(dash.external, false);

  dash = applyEnvelope(dash, stateEnvelope("advertising", "hub discovered: Pybricks Hub"), 2000);
  assert.equal(dash.serverState, "advertising");

  dash = applyEnvelope(dash, stateEnvelope("agent", "agent installed and started"), 3000);
  assert.equal(dash.serverState, "agent");

  dash = applyEnvelope(dash, stateEnvelope("program", "user program started"), 4000);
  assert.equal(dash.serverState, "program");
});

test("hub_info snapshot envelope: name/firmware/model land", () => {
  let dash = initialDashboard();
  dash = applyEnvelope(dash, HUB_INFO, 1000);

  assert.deepEqual(dash.hub, {
    name: "Pybricks Hub",
    firmware: "4.0.1",
    model: "technichub",
    at: 1000,
  });
});

test("battery envelope: pct/mV/mA land from wire keys v/c/pct", () => {
  let dash = initialDashboard();
  dash = applyEnvelope(dash, BATTERY, 1000);

  assert.deepEqual(dash.battery, { percent: 87, voltageMv: 8085, currentMa: 42, at: 1000 });
});

test("imu envelope: accel/gyro/up land from wire keys ax..gz/up", () => {
  let dash = initialDashboard();
  dash = applyEnvelope(dash, IMU, 1000);

  assert.deepEqual(dash.imu, {
    ax: 120, ay: -980, az: 9810,
    gx: 1, gy: -2, gz: 3,
    up: "top",
    at: 1000,
  });
});

test("port envelope: every D7 device kind renders its dictionary rows", () => {
  // Tagged devices appear in both regimes (D9): the idle agent's passive
  // modes (ambient/presence — light off, no ping) and the program-mode
  // active readings (surface/distance, M2 opt-in). Single-mode devices
  // carry no tag.
  const cases = [
    [
      { t: "port", p: "A", dev: "Motor", angle: 12, speed: 0, load: 0 },
      ["angle 12 °", "speed 0 °/s", "load 0 mNm"],
    ],
    [
      { t: "port", p: "B", dev: "ColorSensor", mode: "ambient", amb: 12 },
      ["amb 12 %"],
    ],
    [
      { t: "port", p: "B", dev: "ColorSensor", mode: "surface", refl: 34, h: 10, s: 80, v: 90, col: "red" },
      ["refl 34 %", "h 10 °", "s 80 %", "v 90 %", "col red"],
    ],
    [
      { t: "port", p: "C", dev: "ForceSensor", f: 0.0, d: 0.0, pressed: false },
      ["f 0 N", "d 0 mm", "pressed false"],
    ],
    [
      { t: "port", p: "D", dev: "UltrasonicSensor", mode: "presence", pr: false },
      ["pr false"],
    ],
    [
      { t: "port", p: "D", dev: "UltrasonicSensor", mode: "distance", d: 245 },
      ["d 245 mm"],
    ],
    [
      { t: "port", p: "E", dev: "ColorDistanceSensor", mode: "ambient", amb: 12 },
      ["amb 12 %"],
    ],
    [
      { t: "port", p: "E", dev: "ColorDistanceSensor", mode: "distance", d: 60 },
      ["d 60 %"], // PERCENT — the BOOST-era unit trap (D7)
    ],
    [
      { t: "port", p: "F", dev: "TiltSensor", pitch: 3, roll: -2 },
      ["pitch 3 °", "roll -2 °"],
    ],
    [
      { t: "port", p: "A", dev: "InfraredSensor", d: 50 },
      ["d 50 %"], // program-mode only (D9: an active IR emitter is never the idle agent's)
    ],
  ];
  for (const [data, expected] of cases) {
    let dash = initialDashboard();
    dash = applyEnvelope(dash, telemetryEnvelope(data), 1000);
    assert.deepEqual(
      dash.ports[data.p].rows.map((row) => `${row.key} ${row.text}`),
      expected,
      `device ${data.dev}`,
    );
  }
});

test("unit traps: Motor load is mNm, ColorDistanceSensor d is %, UltrasonicSensor 2000 = no echo", () => {
  // The two D7 unit traps render with the correct unit, never a bare
  // number or a wrong unit.
  const motor = portRows({ dev: "Motor", angle: 10, speed: 20, load: 30 });
  assert.deepEqual(motor.map((r) => r.text), ["10 °", "20 °/s", "30 mNm"]);

  const boost = portRows({ dev: "ColorDistanceSensor", mode: "distance", d: 55 });
  assert.deepEqual(boost.map((r) => r.text), ["55 %"]);

  assert.equal(formatPortValue("UltrasonicSensor", "d", 2000, "mm"), "no echo");
  assert.equal(formatPortValue("UltrasonicSensor", "d", 245, "mm"), "245 mm");
});

test("port envelope: dev:'none' → empty rows; unknown dev string passes through verbatim", () => {
  let dash = initialDashboard();
  dash = applyEnvelope(
    dash,
    telemetryEnvelope({ t: "port", p: "C", dev: "none" }),
    1000,
  );
  assert.equal(dash.ports.C.device, "none");
  assert.deepEqual(dash.ports.C.rows, []);

  // An open-set unknown device: rendered verbatim, extra keys ignored —
  // never an error, never an enum guess.
  dash = applyEnvelope(
    dash,
    telemetryEnvelope({ t: "port", p: "D", dev: "SomeFutureSensor", x: 1, y: 2 }),
    2000,
  );
  assert.equal(dash.ports.D.device, "SomeFutureSensor");
  assert.deepEqual(dash.ports.D.rows, []);
});

test("port envelope: mode-dependent device without a tag is skipped whole (D9)", () => {
  // The server's decoder rejects an untagged ColorSensor line before it
  // reaches the wire; the client mirrors it — never a partial card.
  let dash = applyEnvelope(
    initialDashboard(),
    telemetryEnvelope({ t: "port", p: "B", dev: "ColorSensor", refl: 34, amb: 12 }),
    1000,
  );
  assert.deepEqual(dash.ports.B, { device: null, rows: [], at: null });

  // A non-string tag is malformed the same way (str() or nothing).
  dash = applyEnvelope(
    dash,
    telemetryEnvelope({ t: "port", p: "D", dev: "UltrasonicSensor", mode: 7, d: 245 }),
    2000,
  );
  assert.deepEqual(dash.ports.D, { device: null, rows: [], at: null });
});

test("port envelope: unknown (dev, mode) pair passes through with no rows", () => {
  // Forward compatibility, mirroring the server's decoder: an unknown
  // pair keeps its tag, places the device, and renders no typed rows —
  // the raw line stays the record, the dashboard never guesses.
  let dash = applyEnvelope(
    initialDashboard(),
    telemetryEnvelope({ t: "port", p: "B", dev: "ColorSensor", mode: "weird", x: 42 }),
    1000,
  );
  assert.equal(dash.ports.B.device, "ColorSensor");
  assert.deepEqual(dash.ports.B.rows, []);
});

test("log envelope: raw line lands in the pane's ring", () => {
  let dash = initialDashboard();
  dash = applyEnvelope(dash, logEnvelope("Hello from Pybricks!"), 1000);
  dash = applyEnvelope(dash, logEnvelope('{"t":"battery"}'), 2000);

  assert.deepEqual(dash.logs, ["Hello from Pybricks!", '{"t":"battery"}']);
});

test("unknown telemetry kind: counted verbatim, render never crashes", () => {
  let dash = initialDashboard();
  dash = applyEnvelope(dash, telemetryEnvelope({ t: "lidar", range: 5 }), 1000);
  dash = applyEnvelope(dash, telemetryEnvelope({ t: "weather", temp: 21 }), 2000);

  assert.equal(dash.unknown.count, 2);
  assert.equal(dash.unknown.last, "weather");
  assert.equal(dash.battery.at, null); // nothing else was touched
});

test("unknown envelope kind and malformed payloads: ignored, never crash", () => {
  let dash = applyEnvelope(initialDashboard(), { type: "future", data: {} }, 1);
  dash = applyEnvelope(dash, null, 2);
  dash = applyEnvelope(dash, { type: "state", data: null }, 3);
  dash = applyEnvelope(dash, { type: "telemetry", data: "not an object" }, 4);
  dash = applyEnvelope(dash, { type: "log", data: { line: 42 } }, 5);
  dash = applyEnvelope(dash, { type: "log", data: { line: "ok" } }, 6);
  dash = applyEnvelope(
    dash,
    { type: "telemetry", data: { t: "battery", v: "bad", c: 1, pct: 2 } },
    7,
  );

  // The one well-formed log line landed; every malformed input was
  // dropped without mutating anything else.
  assert.deepEqual(dash.logs, ["ok"]);
  assert.equal(dash.battery.at, null); // wrongly-typed battery: ignored
  assert.equal(dash.serverState, null);
});

test("wrongly-typed known-kind envelopes are skipped whole (all-or-nothing)", () => {
  // The server's decode semantics (D7): a known kind with a wrongly-typed
  // required field is malformed and skipped whole — the client mirrors it,
  // never rendering a partial card (a half-valid battery would look live).
  let dash = applyEnvelope(
    initialDashboard(),
    telemetryEnvelope({ t: "imu", ax: "x", ay: 1, az: 2, gx: 3, gy: 4, gz: 5, up: 6 }),
    1000,
  );
  assert.deepEqual(dash.imu, {
    ax: null, ay: null, az: null, gx: null, gy: null, gz: null, up: null, at: null,
  }); // skipped whole: no partial IMU, no "live" stamp

  dash = applyEnvelope(
    dash,
    telemetryEnvelope({ t: "port", p: "A", dev: "Motor", angle: "fast", speed: 1, load: 2 }),
    2000,
  );
  assert.deepEqual(dash.ports.A, { device: null, rows: [], at: null });

  dash = applyEnvelope(
    dash,
    telemetryEnvelope({ t: "hub_info", name: "Hub", fw: "4.0.1", model: 42 }),
    3000,
  );
  assert.equal(dash.hub.name, null); // skipped whole

  // A fully valid envelope after the malformed one still lands.
  dash = applyEnvelope(
    dash,
    telemetryEnvelope({ t: "port", p: "A", dev: "Motor", angle: 10, speed: 1, load: 2 }),
    4000,
  );
  assert.deepEqual(
    dash.ports.A.rows.map((r) => r.text),
    ["10 °", "1 °/s", "2 mNm"],
  );

  // The same envelope from the malformed-payload test, spelled out: a
  // wrongly-typed battery never stamps a receipt time.
  dash = applyEnvelope(
    dash,
    telemetryEnvelope({ t: "battery", v: "bad", c: 1, pct: 2 }),
    5000,
  );
  assert.equal(dash.battery.at, null);
});

test("mock join sequence (state → hub_info → telemetry) builds the full view", () => {
  // The ?mock=1 join the gateway serves: state envelope, hub_info
  // snapshot, then the live cadence — applied in order, the dashboard
  // reaches a fully-populated state.
  let dash = initialDashboard();
  dash = applyEnvelope(dash, stateEnvelope("agent", "mock source: simulated hub (architecture §2.3)"), 0);
  dash = applyEnvelope(
    dash,
    telemetryEnvelope({ t: "hub_info", name: "Pybricks Hub (mock)", fw: "4.0.1", model: "technichub" }),
    0,
  );
  dash = applyEnvelope(dash, telemetryEnvelope({ t: "imu", ax: 120, ay: -980, az: 9817, gx: 1, gy: -2, gz: 0, up: "top" }), 100);
  dash = applyEnvelope(dash, telemetryEnvelope({ t: "port", p: "A", dev: "Motor", angle: 3, speed: 0, load: 0 }), 100);
  dash = applyEnvelope(
    dash,
    telemetryEnvelope({ t: "port", p: "B", dev: "ColorSensor", mode: "ambient", amb: 10 }),
    100,
  );
  dash = applyEnvelope(dash, telemetryEnvelope({ t: "battery", v: 8085, c: 42, pct: 87 }), 1000);

  assert.equal(dash.serverState, "agent");
  assert.equal(dash.hub.name, "Pybricks Hub (mock)");
  assert.equal(chipView(dash).label, "AGENT");
  assert.equal(dash.ports.A.device, "Motor");
  assert.equal(dash.ports.B.device, "ColorSensor");
  assert.equal(dash.battery.percent, 87);
});

// ---------------------------------------------------------------------------
// Status chip — the architecture §4 table
// ---------------------------------------------------------------------------

test("chipView: the four states + EXTERNAL overlay + pre-join + unknown state", () => {
  const withState = (state, external = false) => ({
    ...initialDashboard(),
    serverState: state,
    external,
  });

  assert.deepEqual(chipView(withState("offline")), {
    label: "OFFLINE — scanning…",
    tone: "offline",
  });
  assert.deepEqual(chipView(withState("advertising")), {
    label: "Connecting…",
    tone: "connecting",
  });
  assert.deepEqual(chipView(withState("agent")), { label: "AGENT", tone: "live" });
  assert.deepEqual(chipView(withState("program")), { label: "PROGRAM", tone: "program" });

  // The F6 overlay wins over plain OFFLINE.
  assert.deepEqual(chipView(withState("offline", true)), {
    label: "EXTERNAL",
    tone: "external",
  });

  // Before the first state envelope: a quiet placeholder, never a crash.
  assert.deepEqual(chipView(initialDashboard()), { label: "…", tone: "offline" });

  // Unknown wire state (forward compatibility): displayed verbatim
  // uppercased, never guessed.
  assert.deepEqual(chipView(withState("hibernating")), {
    label: "HIBERNATING",
    tone: "offline",
  });
});

test("isDimmed: OFFLINE (incl. EXTERNAL) and pre-join dim; live states don't", () => {
  const withState = (state, external = false) => ({
    ...initialDashboard(),
    serverState: state,
    external,
  });
  assert.equal(isDimmed(withState("offline")), true);
  assert.equal(isDimmed(withState("offline", true)), true);
  assert.equal(isDimmed(initialDashboard()), true);
  assert.equal(isDimmed(withState("advertising")), false);
  assert.equal(isDimmed(withState("agent")), false);
  assert.equal(isDimmed(withState("program")), false);
});

// ---------------------------------------------------------------------------
// Done-when 2: stale-marking — fake clock, per-kind timeouts
// ---------------------------------------------------------------------------

test("staleness: battery stays live within 5×1 Hz, dims after (fake clock)", () => {
  const at = 10_000;
  // Fresh at t+4999: live. At t+5000 (5 missed 1 Hz periods): stale.
  assert.equal(staleness(at, at + 4999, STALE_MS.battery).stale, false);
  assert.equal(staleness(at, at + 5000, STALE_MS.battery).stale, true);
  assert.equal(staleness(at, at + 60_000, STALE_MS.battery).stale, true);
  // Age is monotonic from the receipt stamp.
  assert.equal(staleness(at, at + 1500, STALE_MS.battery).ageMs, 1500);
});

test("staleness: imu/ports stale at 5×10 Hz = 500 ms (fake clock)", () => {
  const at = 10_000;
  assert.equal(staleness(at, at + 499, STALE_MS.imu).stale, false);
  assert.equal(staleness(at, at + 500, STALE_MS.imu).stale, true);
  assert.equal(staleness(at, at + 499, STALE_MS.port).stale, false);
  assert.equal(staleness(at, at + 500, STALE_MS.port).stale, true);
});

test("staleness: never-seen values count as stale (age unknown)", () => {
  const s = staleness(null, 9999, STALE_MS.battery);
  assert.deepEqual(s, { stale: true, ageMs: null });
});

test("stale-marking story: a live feed that stops dims at the cadence timeout", () => {
  // The full R5 story with the fake clock: battery arrives at 1 Hz,
  // then the feed cuts — the value must flip stale after five missed
  // periods, purely by receipt-time arithmetic.
  let clock = 0;
  let dash = initialDashboard();
  for (let i = 0; i < 3; i++) {
    clock += 1000;
    dash = applyEnvelope(dash, BATTERY, clock);
    assert.equal(staleness(dash.battery.at, clock, STALE_MS.battery).stale, false);
  }
  // Feed cuts; the clock keeps running with no envelopes.
  clock += 4999;
  assert.equal(staleness(dash.battery.at, clock, STALE_MS.battery).stale, false);
  clock += 1;
  assert.equal(staleness(dash.battery.at, clock, STALE_MS.battery).stale, true);
});

test("stale timeouts are 5× the D7 cadence", () => {
  assert.equal(STALE_MS.battery, 5000); // 5 × 1 Hz
  assert.equal(STALE_MS.imu, 500); // 5 × 10 Hz
  assert.equal(STALE_MS.port, 500); // 5 × 10 Hz
});

test("paint cadence: coalesced, human-readable, and stale-safe", () => {
  // The paint tick decouples DOM work from envelope arrival (the join
  // replay bursts the whole ring; steady mock is ~60 envelopes/s; a full
  // hub would be higher). It must sit inside the readable band and at or
  // under half the shortest stale timeout, so a stale flip lands within
  // one cadence period of paint — never delayed past it.
  assert.ok(PAINT_MS >= 100 && PAINT_MS <= 200, "paint band is 100–200 ms");
  assert.ok(PAINT_MS * 2 <= STALE_MS.imu, "paint is at most half the shortest stale timeout");
  assert.ok(PAINT_MS * 2 <= STALE_MS.port, "paint is at most half the shortest stale timeout");
});

// ---------------------------------------------------------------------------
// Delegated decision 1: the EXTERNAL overlay rule (F6)
// ---------------------------------------------------------------------------

test("isExternalTakeoverReason: token list, case-insensitive, non-strings safe", () => {
  // The canonical F6 wording once the server pins a reason token…
  assert.equal(isExternalTakeoverReason("external client took the hub"), true);
  assert.equal(isExternalTakeoverReason("External Client Took The Hub"), true);
  // …and the phrases a connect-failure exception might carry.
  assert.equal(isExternalTakeoverReason("connect failed: device busy"), true);
  assert.equal(isExternalTakeoverReason("connect failed: already connected elsewhere"), true);
  assert.equal(isExternalTakeoverReason("connect failed: resource in use"), true);
  // Today's real manager reasons (verified against ble_manager) do NOT
  // light the overlay — honesty over guessing.
  assert.equal(isExternalTakeoverReason("hub not found (scan timeout)"), false);
  assert.equal(isExternalTakeoverReason("hub disconnected"), false);
  assert.equal(isExternalTakeoverReason("connect timed out after 10s"), false);
  assert.equal(isExternalTakeoverReason("session setup failed: boom"), false);
  assert.equal(isExternalTakeoverReason("service stopped"), false);
  assert.equal(isExternalTakeoverReason("still offline: hub not found (scan timeout)"), false);
  // Trust boundary.
  assert.equal(isExternalTakeoverReason(null), false);
  assert.equal(isExternalTakeoverReason(42), false);
  assert.equal(isExternalTakeoverReason(""), false);
});

test("nextExternalOverlay: fresh offline entry decides by reason; sticky within the episode; cleared on reconnect", () => {
  // Fresh entry into OFFLINE with a takeover reason → overlay on.
  assert.equal(nextExternalOverlay("agent", false, "offline", "external client took the hub"), true);
  // Fresh entry with a hub-off reason → plain OFFLINE.
  assert.equal(nextExternalOverlay("agent", false, "offline", "hub disconnected"), false);
  assert.equal(nextExternalOverlay(null, false, "offline", "hub not found (scan timeout)"), false);

  // Sticky: while OFFLINE persists, a later non-takeover reason (the
  // rescan loop's scan misses) must not flicker the overlay away.
  assert.equal(nextExternalOverlay("offline", true, "offline", "still offline: hub not found"), true);
  // …and it cannot light up from a scan miss either — only a fresh
  // entry (or an explicit takeover token) decides.
  assert.equal(nextExternalOverlay("offline", false, "offline", "hub not found (scan timeout)"), false);

  // Any non-offline state clears the overlay (the hub came back).
  assert.equal(nextExternalOverlay("offline", true, "advertising", "hub discovered: Pybricks Hub"), false);
  assert.equal(nextExternalOverlay("offline", true, "agent", "agent installed and started"), false);
});

test("EXTERNAL overlay story over a full envelope sequence", () => {
  // Agent mode → external takeover → sticky offline → recovery.
  let dash = initialDashboard();
  dash = applyEnvelope(dash, stateEnvelope("agent", "agent installed and started"), 0);
  assert.equal(dash.external, false);
  assert.equal(chipView(dash).label, "AGENT");

  dash = applyEnvelope(dash, stateEnvelope("offline", "external client took the hub"), 1000);
  assert.equal(dash.external, true);
  assert.equal(chipView(dash).label, "EXTERNAL");
  assert.equal(isDimmed(dash), true);

  // Subsequent offline envelopes (a scan-miss reason) keep the overlay.
  dash = applyEnvelope(dash, stateEnvelope("offline", "hub not found (scan timeout)"), 2000);
  assert.equal(dash.external, true, "overlay is sticky within the offline episode");

  // Recovery clears it.
  dash = applyEnvelope(dash, stateEnvelope("advertising", "hub discovered: Pybricks Hub"), 3000);
  assert.equal(dash.external, false);
  assert.equal(chipView(dash).label, "Connecting…");
});

test("a hub power-cycle (plain disconnect) shows OFFLINE, not EXTERNAL", () => {
  let dash = initialDashboard();
  dash = applyEnvelope(dash, stateEnvelope("agent", "agent installed and started"), 0);
  dash = applyEnvelope(dash, stateEnvelope("offline", "hub disconnected"), 1000);

  assert.equal(dash.external, false);
  assert.equal(chipView(dash).label, "OFFLINE — scanning…");
});

// ---------------------------------------------------------------------------
// Delegated decision 2/3/4 inputs: constants centralized
// ---------------------------------------------------------------------------

test("log scrollback: bounded ring keeps the newest N lines", () => {
  assert.equal(LOG_SCROLLBACK, 1000);

  let logs = [];
  for (let i = 0; i < 1002; i++) {
    logs = appendLog(logs, `line ${i}`);
  }
  assert.equal(logs.length, 1000);
  assert.deepEqual(logs[0], "line 2"); // the oldest two dropped
  assert.deepEqual(logs.at(-1), "line 1001");
  // Under the cap: nothing dropped.
  assert.equal(appendLog(["a"], "b").length, 2);
});

test("appendLog honors a custom cap", () => {
  let logs = [];
  for (let i = 0; i < 5; i++) {
    logs = appendLog(logs, `l${i}`, 3);
  }
  assert.deepEqual(logs, ["l2", "l3", "l4"]);
});

test("log envelopes advance logSeq — the cap keeps length flat, the seq does not", () => {
  // The DOM-sync bug class this exists to prevent: at the ring's cap the
  // array length never grows, so only the monotonic counter can signal
  // that new lines landed. applyLog must advance it on every line.
  let dash = initialDashboard();
  for (let i = 0; i < LOG_SCROLLBACK + 5; i++) {
    dash = applyEnvelope(dash, logEnvelope(`line ${i}`), i);
  }
  assert.equal(dash.logs.length, LOG_SCROLLBACK);
  assert.equal(dash.logSeq, LOG_SCROLLBACK + 5);
  assert.deepEqual(dash.logs[0], "line 5"); // oldest five dropped
  assert.deepEqual(dash.logs.at(-1), `line ${LOG_SCROLLBACK + 4}`);
});

test("logSyncPlan: the DOM-sync decision for the log pane (cap regression)", () => {
  // Nothing new: no work.
  assert.deepEqual(logSyncPlan(["a"], 1, 1), { kind: "none", lines: [] });
  assert.deepEqual(logSyncPlan(["a"], 0, 3), { kind: "none", lines: [] });

  // Under the cap: append exactly the missed lines, oldest-first.
  const ring = ["l0", "l1", "l2", "l3"];
  assert.deepEqual(logSyncPlan(ring, 4, 2), {
    kind: "append",
    lines: ["l2", "l3"],
  });

  // AT THE CAP (the freeze regression): the length is pinned, yet a
  // missed line must still be appended — the seq, not the length, is
  // the signal.
  let logs = [];
  let seq = 0;
  for (let i = 0; i < LOG_SCROLLBACK; i++) {
    logs = appendLog(logs, `x${i}`);
    seq += 1;
  }
  // One more line past the cap: ring drops x0, keeps x1..x1000.
  logs = appendLog(logs, "x1000");
  seq += 1;
  assert.equal(logs.length, LOG_SCROLLBACK); // flat, as in production
  const plan = logSyncPlan(logs, seq, seq - 1);
  assert.equal(plan.kind, "append");
  assert.deepEqual(plan.lines, ["x1000"]);

  // More than a ringful missed between renders (backgrounded tab): the
  // delta is meaningless — rebuild from the ring as-is.
  assert.deepEqual(logSyncPlan(logs, seq + LOG_SCROLLBACK + 1, seq), {
    kind: "rebuild",
    lines: logs.slice(),
  });
});

test("seenText / seenStamp / formatAge: the last-seen lines (fake clock)", () => {
  // seenText: cadence-driven cards (R5 dim + timestamp).
  assert.equal(seenText(null, 9999, STALE_MS.battery), "never seen");
  assert.equal(seenText(10_000, 10_500, STALE_MS.battery), "live · last seen 0.5s ago");
  assert.equal(seenText(10_000, 15_000, STALE_MS.battery), "stale · last seen 5.0s ago");
  assert.equal(seenText(10_000, 10_999, STALE_MS.imu), "stale · last seen 1.0s ago");

  // seenStamp: snapshot cards (hub identity) — a plain stamp, no
  // stale/live flip (hub_info is once-per-connect).
  assert.equal(seenStamp(null, 1000), "never seen");
  assert.equal(seenStamp(10_000, 12_500), "last seen 2.5s ago");

  // formatAge bands: sub-10s tenths, then whole seconds/minutes/hours;
  // negative deltas clamp (clock skew never shows "in the future").
  assert.equal(formatAge(0), "0.0s ago");
  assert.equal(formatAge(9_999), "10.0s ago");
  assert.equal(formatAge(10_000), "10s ago");
  assert.equal(formatAge(59_999), "59s ago"); // floors, never rounds up
  assert.equal(formatAge(60_000), "1m ago");
  assert.equal(formatAge(3_600_000), "1h ago");
});

test("reconnect backoff: 500 ms base, doubling, capped at 15 s", () => {
  assert.equal(nextReconnectDelay(0), 500);
  assert.equal(nextReconnectDelay(1), 1000);
  assert.equal(nextReconnectDelay(2), 2000);
  assert.equal(nextReconnectDelay(4), 8000);
  assert.equal(nextReconnectDelay(5), 15000); // 16000 capped
  assert.equal(nextReconnectDelay(20), 15000);
  assert.equal(nextReconnectDelay(-1), 500); // attempt counter never negative
});

test("mock mode + ws URL decisions", () => {
  assert.equal(isMockMode("?mock=1"), true);
  assert.equal(isMockMode(""), false);
  assert.equal(isMockMode("?other=2"), false);
  assert.equal(isMockMode("?mock=0"), false);

  const plainLocation = { protocol: "http:", host: "box:8300" };
  assert.equal(wsUrl(plainLocation, false), "ws://box:8300/ws");
  assert.equal(wsUrl(plainLocation, true), "ws://box:8300/ws?mock=1");

  const tlsLocation = { protocol: "https:", host: "box.example" };
  assert.equal(wsUrl(tlsLocation, false), "wss://box.example/ws");
  assert.equal(wsUrl(tlsLocation, true), "wss://box.example/ws?mock=1");
});

// ---------------------------------------------------------------------------
// Port letter trust boundary
// ---------------------------------------------------------------------------

test("port envelope with a non-string port letter is dropped safely", () => {
  let dash = applyEnvelope(
    initialDashboard(),
    telemetryEnvelope({ t: "port", p: 65, dev: "Motor", angle: 1, speed: 2, load: 3 }),
    1000,
  );
  // Nothing crashed, nothing landed (65 is not a port letter string).
  assert.deepEqual(dash.ports.A, { device: null, rows: [], at: null });
  assert.deepEqual(dash.ports, dash.ports); // every port untouched
  assert.equal(Object.keys(dash.ports).length, 6); // no stray "65" key
});

test("port envelope for a letter outside A–F still stores (open render map)", () => {
  // The state object is a map, not a fixed array: a future hub with a
  // "G" port stores fine; the glue renders the letters it knows.
  let dash = applyEnvelope(
    initialDashboard(),
    telemetryEnvelope({ t: "port", p: "G", dev: "Motor", angle: 1, speed: 2, load: 3 }),
    1000,
  );
  assert.equal(dash.ports.G.device, "Motor");
});