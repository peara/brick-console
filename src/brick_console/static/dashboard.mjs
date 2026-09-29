// brick-console dashboard — pure render core (R2; D7 wire contract).
//
// ZERO DOM in this module: every function maps WS envelope JSON (the D7
// envelope shapes the WS gateway ships) to plain view-model data, or
// computes a decision the DOM glue needs. The glue (main.js) imports this
// module and never derives values itself; the unit tests (tests/js/)
// import the same module under `node --test` with a fake clock — no
// browser, no DOM, no build step (architecture §8).
//
// Wire facts this module codes against (D7 + the WS gateway):
// - Envelope: {"type": "state"|"telemetry"|"log", "hub": <label>, "data": …}.
// - state data: {"state": "offline"|"advertising"|"agent"|"program"
//   (lowercase wire values; the chip displays them per architecture §4),
//   "reason": free-form server string}.
// - telemetry data: the D7 wire object verbatim; the kind key "t" lives
//   inside data: "hub_info" | "battery" | "imu" | "port" | <unknown>.
// - log data: {"src": "stdout", "line": "…"} — raw stdout lines; the join
//   replay never includes logs, so the pane fills live only.
// - NO timestamps anywhere on the wire: stale-marking and "last seen" key
//   off CLIENT receipt time (the nowMs argument, Date.now() in the glue).
//
// Robustness rule: the WS input is a trust boundary. Unknown envelope
// kinds, unknown telemetry kinds, unknown device strings, and
// wrong-typed fields never crash the render — they are ignored or passed
// through verbatim (D7: versioned by addition; `dev` is an open set).

export const STALE_MS = Object.freeze({
  // Per-kind stale timeouts: 5× the D7 cadence (battery ~1 Hz, imu/ports
  // ~10 Hz) — five missed periods means the feed is gone, not just late
  // (R5's stale-marking rule; the values dim, never vanish).
  battery: 5000,
  imu: 500,
  port: 500,
});

export const LOG_SCROLLBACK = 1000;
// Client-side raw-log pane bound: the last 1000 stdout lines. Generous
// scrollback for a demo/README-reading session, still a hard cap so a
// days-open tab cannot grow the DOM without bound (the server's own
// raw-line ring is bounded separately).

export const RECONNECT = Object.freeze({ baseMs: 500, maxMs: 15000 });
// WS reconnect backoff: first retry after 500 ms, doubling per failed
// attempt, capped at 15 s — mirrors the manager's bounded rescan posture
// (architecture §4 rule 1: never fight, but always come back).

// ---------------------------------------------------------------------------
// EXTERNAL overlay (F6) — a UI overlay on OFFLINE, derived client-side
// ---------------------------------------------------------------------------

const EXTERNAL_REASON_TOKENS = [
  "external", // canonical F6 wording once the server pins a reason token
  "took the hub",
  "another client",
  "another central",
  "another device",
  "in use",
  "busy",
  "already connected",
];

// True when a free-form server disconnect reason indicates the hub was
// taken over by an external BLE central (F6). Case-insensitive substring
// match over the token list: the reason strings are free-form server
// prose (no canonical F6 token exists on the wire yet), so this is a
// heuristic seam — a later server-side change that pins a canonical
// reason token lights the overlay up without any client change.
// Known limitation (documented deliberately): today's manager reasons
// ("hub not found (scan timeout)", "connect failed: <exc>",
// "hub disconnected", "connect timed out after <N>s",
// "session setup failed: <exc>", "service stopped") match none of these
// tokens except by accident inside a wrapped connect-failure exception,
// so a takeover currently shows as plain OFFLINE until that server change
// lands. Matching is honest, never guessed: no match, no overlay.
export function isExternalTakeoverReason(reason) {
  if (typeof reason !== "string") return false;
  const haystack = reason.toLowerCase();
  return EXTERNAL_REASON_TOKENS.some((token) => haystack.includes(token));
}

// Advance the EXTERNAL overlay across one state envelope. Pure; the glue
// feeds it the previous state and the previous overlay value, so the
// overlay has a stable basis across envelopes:
// - a FRESH entry into OFFLINE decides by its disconnect reason (external
//   token → overlay on; hub-off / scan-timeout → plain OFFLINE);
// - while OFFLINE persists, the rescan loop's subsequent reasons ("still
//   offline: hub not found …") must not flicker the overlay away — it is
//   sticky within one offline episode;
// - any non-offline state (advertising/agent/program) clears it.
export function nextExternalOverlay(prevState, prevExternal, state, reason) {
  if (state !== "offline") return false;
  if (prevState !== "offline") return isExternalTakeoverReason(reason);
  return prevExternal || isExternalTakeoverReason(reason);
}

// ---------------------------------------------------------------------------
// Connection URL decisions
// ---------------------------------------------------------------------------

// ?mock=1 (exact value, like the WS gateway's own query-param parse):
// the page is the same file either way — only the WS URL differs.
export function isMockMode(search) {
  return new URLSearchParams(search).get("mock") === "1";
}

// The /ws URL for this page: wss on https pages, ws otherwise; ?mock=1
// rides along so the gateway serves the in-process synthetic source
// (architecture §2.3 — develop and demo with the hub off).
export function wsUrl(location, mock) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const query = mock ? "?mock=1" : "";
  return `${protocol}//${location.host}/ws${query}`;
}

// Reconnect delay for attempt N (0-based): base × 2^N, capped at max.
// Pure and deterministic — the glue may layer jitter, the policy lives here.
export function nextReconnectDelay(attempt, { baseMs, maxMs } = RECONNECT) {
  const bounded = Math.min(Math.max(attempt, 0), 30); // 2^30 ms overflow guard
  return Math.min(maxMs, baseMs * 2 ** bounded);
}

// ---------------------------------------------------------------------------
// Port field dictionary (D7): wire key, display unit, and the JSON type
// the wire allows ("number" | "boolean" | "string" — the same validator
// tags the server's decoder enforces; a wrong-typed field is dropped, not
// rendered as "undefined"). The BOOST-era unit trap: ColorDistanceSensor
// `d` is PERCENT, not mm (D7); Motor `load` is mNm, not a percentage.
const T_NUMBER = "number";
const T_BOOLEAN = "boolean";
const T_STRING = "string";

export const PORT_FIELDS = Object.freeze({
  Motor: [
    ["angle", "°", T_NUMBER],
    ["speed", "°/s", T_NUMBER],
    ["load", "mNm", T_NUMBER],
  ],
  ColorSensor: [
    ["refl", "%", T_NUMBER],
    ["amb", "%", T_NUMBER],
    ["h", "°", T_NUMBER],
    ["s", "%", T_NUMBER],
    ["v", "%", T_NUMBER],
    ["col", "", T_STRING],
  ],
  ForceSensor: [
    ["f", "N", T_NUMBER],
    ["d", "mm", T_NUMBER],
    ["pressed", "", T_BOOLEAN],
  ],
  UltrasonicSensor: [
    ["d", "mm", T_NUMBER],
    ["pr", "", T_BOOLEAN],
  ],
  ColorDistanceSensor: [
    ["d", "%", T_NUMBER], // PERCENT — the BOOST-era unit trap, not mm (D7)
    ["refl", "%", T_NUMBER],
    ["amb", "%", T_NUMBER],
    ["h", "°", T_NUMBER],
    ["s", "%", T_NUMBER],
    ["v", "%", T_NUMBER],
    ["col", "", T_STRING],
  ],
  TiltSensor: [
    ["pitch", "°", T_NUMBER],
    ["roll", "°", T_NUMBER],
  ],
  InfraredSensor: [["d", "%", T_NUMBER]],
  // "none" — the empty port — has no rows (not listed; open set).
});

// Format one port value for display. Pure value → string mapping:
// numbers gain their dictionary unit, booleans/strings pass verbatim,
// and the UltrasonicSensor's 2000 mm no-echo sentinel renders as words
// instead of a misleading "2000 mm" (D7 field dictionary).
export function formatPortValue(device, key, value, unit) {
  if (device === "UltrasonicSensor" && key === "d" && value === 2000) {
    return "no echo";
  }
  if (typeof value === "boolean") return String(value);
  if (typeof value === "number") return unit ? `${value} ${unit}` : String(value);
  return String(value);
}

// Append one raw line to the bounded scrollback ring. Pure (returns a new
// array); overflow drops the OLDEST lines — the pane keeps the newest N.
export function appendLog(logs, line, cap = LOG_SCROLLBACK) {
  const next = logs.concat([line]);
  return next.length > cap ? next.slice(next.length - cap) : next;
}

// ---------------------------------------------------------------------------
// Dashboard state — envelope application (pure: returns a new state)
// ---------------------------------------------------------------------------

export function initialDashboard() {
  const ports = {};
  for (const letter of ["A", "B", "C", "D", "E", "F"]) {
    ports[letter] = { device: null, rows: [], at: null };
  }
  return {
    serverState: null, // latest wire state ("offline"|"advertising"|"agent"|"program")
    reason: "",
    external: false, // the F6 overlay (derived, never on the wire)
    hub: { name: null, firmware: null, model: null, at: null },
    battery: { percent: null, voltageMv: null, currentMa: null, at: null },
    imu: {
      ax: null, ay: null, az: null,
      gx: null, gy: null, gz: null,
      up: null, at: null,
    },
    ports, // letter → {device, rows, at}
    logs: [], // bounded raw-line ring (client-side scrollback)
    unknown: { count: 0, last: null }, // unknown telemetry kinds (ignored, counted)
  };
}

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function num(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value) {
  return typeof value === "string" ? value : null;
}

// True when `value`'s JSON type satisfies the wire's validator tag (D7):
// numbers never accept booleans, strings are never coerced — the client
// mirrors the server's decode strictness ("never guesses").
function typeOk(tag, value) {
  if (tag === T_NUMBER) return typeof value === "number" && Number.isFinite(value);
  if (tag === T_BOOLEAN) return typeof value === "boolean";
  return typeof value === "string";
}

// Rows for one `port` wire object: the D7 dictionary fields for a known
// device, in canonical order. Unknown device strings (open set) yield NO
// rows — the glue renders the dev string verbatim and the extra keys are
// ignored (D7 passthrough policy); "none" likewise yields no rows and the
// glue dims the chip as an empty port. Wrong-typed fields are dropped,
// never rendered as "undefined".
export function portRows(data) {
  const device = typeof data?.dev === "string" ? data.dev : "";
  const spec = PORT_FIELDS[device];
  if (!spec) return [];
  const rows = [];
  for (const [key, unit, tag] of spec) {
    const value = data[key];
    if (typeOk(tag, value)) {
      rows.push({ key, text: formatPortValue(device, key, value, unit) });
    }
  }
  return rows;
}

// Apply one WS envelope to the dashboard state. `nowMs` is the CLIENT
// receipt time (Date.now() in the glue; a fake clock in tests) — the wire
// never carries timestamps, so every "last seen"/staleness decision keys
// off this stamp. Unknown or malformed input leaves the state unchanged
// (render gracefully, never crash).
export function applyEnvelope(dash, envelope, nowMs) {
  if (!isObject(envelope) || !isObject(envelope.data)) return dash;
  const { type, data } = envelope;
  if (type === "state") return applyState(dash, data);
  if (type === "telemetry") {
    // Only a real telemetry kind counts as unknown: a non-string "t" is
    // malformed, not a forward-compatible kind (the server's decoder
    // raises on a missing/non-string "t"; the client drops it).
    if (typeof data.t !== "string") return dash;
    return applyTelemetry(dash, data, nowMs);
  }
  if (type === "log") return applyLog(dash, data);
  return dash; // unknown envelope kind: ignored (D7: versioned by addition)
}

function applyState(dash, data) {
  const state = str(data.state);
  // A state envelope without a string state is malformed: drop it whole
  // rather than half-apply (a dropped reason must not flicker the F6
  // overlay's basis).
  if (state === null) return dash;
  const reason = str(data.reason) ?? "";
  return {
    ...dash,
    serverState: state,
    reason,
    external: nextExternalOverlay(dash.serverState, dash.external, state, reason),
  };
}

function applyTelemetry(dash, data, nowMs) {
  switch (data.t) {
    case "hub_info": {
      const name = str(data.name);
      const firmware = str(data.fw);
      const model = str(data.model);
      // All-or-nothing (the server's decode semantics: a known kind with
      // a wrongly-typed required field is malformed and skipped whole —
      // the client never guesses and never renders a partial card).
      if (name === null || firmware === null || model === null) return dash;
      return {
        ...dash,
        hub: { name, firmware, model, at: nowMs },
      };
    }
    case "battery": {
      const percent = num(data.pct);
      const voltageMv = num(data.v);
      const currentMa = num(data.c);
      if (percent === null || voltageMv === null || currentMa === null) return dash;
      return {
        ...dash,
        battery: { percent, voltageMv, currentMa, at: nowMs },
      };
    }
    case "imu": {
      const ax = num(data.ax), ay = num(data.ay), az = num(data.az);
      const gx = num(data.gx), gy = num(data.gy), gz = num(data.gz);
      const up = str(data.up);
      if (
        ax === null || ay === null || az === null ||
        gx === null || gy === null || gz === null || up === null
      ) {
        return dash;
      }
      return {
        ...dash,
        imu: { ax, ay, az, gx, gy, gz, up, at: nowMs },
      };
    }
    case "port": {
      const letter = str(data.p);
      if (letter === null) return dash; // no port letter: nothing to place
      const device = str(data.dev);
      if (device === null) return dash;
      // All-or-nothing per the D7 dictionary: a known device with a
      // wrongly-typed field is malformed — skipped whole, like the
      // server's decoder. Unknown devices ("none" included) have no
      // required fields and always place.
      const spec = PORT_FIELDS[device];
      if (spec) {
        for (const [key, , tag] of spec) {
          if (!typeOk(tag, data[key])) return dash;
        }
      }
      const ports = {
        ...dash.ports,
        [letter]: { device, rows: portRows(data), at: nowMs },
      };
      return { ...dash, ports };
    }
    default: {
      // Unknown telemetry kind (D7 UnknownEvent): arrives verbatim;
      // count + remember it, render nothing, never crash.
      return {
        ...dash,
        unknown: { count: dash.unknown.count + 1, last: str(data.t) },
      };
    }
  }
}

function applyLog(dash, data) {
  if (typeof data.line !== "string") return dash;
  return { ...dash, logs: appendLog(dash.logs, data.line) };
}

// ---------------------------------------------------------------------------
// Views — pure derivations for the DOM glue
// ---------------------------------------------------------------------------

// The status chip (architecture §4 table): four values plus the F6 EXTERNAL
// overlay on OFFLINE. Wire values are lowercase; the chip displays
// uppercase per the state model. ADVERTISING shows "Connecting…" while the
// connect attempt is in flight; OFFLINE shows "OFFLINE — scanning…" (the
// service's bounded rescan loop is always running, R1/F6).
export function chipView(dash) {
  if (dash.external) return { label: "EXTERNAL", tone: "external" };
  const state = dash.serverState;
  if (state === "offline") return { label: "OFFLINE — scanning…", tone: "offline" };
  if (state === "advertising") return { label: "Connecting…", tone: "connecting" };
  if (state === "agent") return { label: "AGENT", tone: "live" };
  if (state === "program") return { label: "PROGRAM", tone: "program" };
  if (typeof state === "string" && state !== "") {
    // Unknown wire state (forward compatibility): display verbatim,
    // uppercased — never crash, never guess.
    return { label: state.toUpperCase(), tone: "offline" };
  }
  return { label: "…", tone: "offline" }; // before the first state envelope
}

// True while the hub cards should gray out (state envelope → all cards):
// OFFLINE (including its EXTERNAL overlay) dims the cards with "last
// seen" timestamps; AGENT/PROGRAM/ADVERTISING keep them live (R5's stale
// marking still dims individual stale values on its own cadence clock).
export function isDimmed(dash) {
  return dash.serverState === null || dash.serverState === "offline";
}

// Staleness of one receipt stamp: `at` is the client receipt time of the
// newest envelope of a kind; `timeoutMs` is the kind's STALE_MS entry
// (5× its D7 cadence). Fresh-then-stale transitions are dim + timestamp
// in the glue; a never-seen value (at === null) counts as stale.
export function staleness(at, nowMs, timeoutMs) {
  if (at === null) return { stale: true, ageMs: null };
  const ageMs = Math.max(0, nowMs - at);
  return { stale: ageMs >= timeoutMs, ageMs };
}