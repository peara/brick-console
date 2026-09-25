# Decision records — brick-console

Short ADR-style log. Each entry: context → decision → consequences. Numbers are stable IDs; superseded decisions stay for history.

## D1 — Telemetry comes from hub-side code, not REPL polling

**Status:** accepted (2026-09-23, reaffirmed 2026-09-24)

**Context:** The dashboard needs live battery/IMU/port/motor/sensor data. Two candidate channels exist: (a) poll the Pybricks REPL over BLE, or (b) run a program on the hub that pushes data.

**Decision:** Hub-side code. The Pybricks BLE profile exposes no ambient telemetry — only static device info, capabilities, and 3 battery *warning flags*; the hub stdio pipe (Pybricks event characteristic on v4.0.1; the Nordic UART Service is the legacy pre-1.3 channel) carries `print()` output and nothing else. Rich data therefore requires a program. REPL polling is rejected because it interrupts whatever program is running (confirmed by pybricks-hub-tester's own caveat).

**Consequences:** Telemetry ships as an importable MicroPython library (`brick_telemetry`, see architecture §2.2) plus a thin agent wrapper the server installs in hub RAM when idle. User programs can opt in by importing the library, keeping the dashboard live in Program mode. The wrapper never occupies the 5 permanent slots.

## D2 — Server-side BLE only; no Web Bluetooth anywhere

**Status:** accepted (2026-09-23)

**Context:** Browser-side BLE (Web Bluetooth) is what pybricks-code, lego-control-center, and pybricks-hub-tester all use. But it runs in the *browser's* machine, so a laptop without its own radio — or with the hub out of its range — gets nothing, and there is no always-on manager when no browser is open.

**Decision:** The Linux box is the sole BLE central. Browsers talk to the server over HTTP/WebSocket. This is the architecture proven by brickrail (114★ train-automation project).

**Consequences:** Zero-BLE laptop experience and unattended auto-reconnect become possible. Cost: we build a custom UI instead of self-hosting existing Web-Bluetooth apps; their UX (esp. lego-control-center) serves as the reference.

## D3 — Python-first; no word-blocks coding in v1

**Status:** accepted (2026-09-23)

**Context:** The retiring LEGO app was blocks-first. Pybricks Code already has an excellent blocks canvas (free, maintained).

**Decision:** brick-console serves Python only in v1.

**Consequences:** Smaller scope; blocks users are pointed at Pybricks Code. Revisit only if blocks ever matter here.

## D4 — v1 telemetry wire: JSON lines over hub stdout

**Status:** accepted (2026-09-23); measurement follow-up promoted to BRD Q4

**Context:** Two candidate wires for agent telemetry: (a) JSON lines via `print()` over hub stdout, or (b) binary packing via the Pybricks `AppData` module (lego-control-center's approach). On our firmware (Pybricks v4.0.1, profile ≥ 1.3) hub stdout travels as `WRITE_STDOUT` events on the Pybricks command/event characteristic (`c5f50002`) — the Nordic UART Service is the legacy pre-1.3 stdio channel, unused here.

**Decision:** Start with (a) — the pattern of the official Pybricks pc-communication tutorial. Debuggable from any REPL client, trivially parseable, no GATT plumbing beyond the Pybricks characteristic notifications.

**Consequences:** Potential throughput overhead vs AppData (JSON is verbose; ~10 Hz × few lines is well within BLE capacity). Q4 in the BRD tracks a measurement during M1; if it disappoints, swap to AppData behind the same server-side event types — the change is contained to `brick_telemetry` + the BLE manager parser.

## D5 — Server is the source of truth for programs; hub storage optional

**Status:** accepted (2026-09-23)

**Context:** The hub has 5 permanent program slots + RAM-run programs. Where should the program library live?

**Decision:** Programs live on the server filesystem (`programs/`), run-to-RAM by default. Permanent slots are a stretch (R9, blocked on Q2 — scriptable slot writes unverified).

**Consequences:** No sync problem, full CRUD over REST, hub power-cycles lose nothing. Hub stays stateless; button-only standalone boot of a chosen program is deferred with R9.

## D-FL — Firmware: Pybricks v4.0.1 stable; LEGO firmware backed up

**Status:** accepted (2026-09-24 — supersedes the draft's "pin v3.x" stance)

**Context:** The draft BRD planned to pin Pybricks v3.x because v4 was beta. Between drafting and execution, Pybricks v4.0.1 went stable (2026-06-24 release; docs.pybricks.com/en/stable now documents v4.0.0).

**Decision:** Flash v4.0.1 stable. Keep the DFU backup of the original LEGO firmware (`~/hermes/m5stack/backups/lego-original-inventor-hub.bin`, 1,015,808 B, md5 d0c76999…) as the recovery path; `pybricksdev dfu restore` reverts any time.

**Consequences:** We build on the current stable line (system.storage, v4 API surface). Upgrades are deliberate and changelog-checked. Note: pybricksdev's `dfu backup` requires system `dfu-util` (its built-in backup is a stub) — installed on the box.

## D-GH — Repository: `peara/brick-console`, private, docs split

**Status:** accepted (2026-09-24)

**Context:** Work outgrew chat-driven notes; a real repo was wanted before M1 coding. Naming: the "hubdock" working title was never loved.

**Decision:** Private GitHub repo `brick-console`. Docs split into brd.md / architecture.md / decisions.md / research archive (frozen v0.1 draft + investigation). Repo is the code home from day one (uv env lives here; `.venv`, firmware binaries, and the LEGO backup are gitignored or kept outside).

**Consequences:** "hubdock" is retired; all references renamed to brick-console / `brick_telemetry`. Task tracking planned as GitHub Issues + milestones mirroring M0–M5 (Notion stays for diary/logs, not project tasks).

## D6 — All hub access goes through the `Transport` interface

**Status:** accepted (2026-09-24 — resolves BRD Q1)

**Context:** The server needs BLE (scan, connect, install+run, stop, stdin/stdout) and pybricksdev 2.3.2 already implements the Pybricks GATT protocol — but its `PybricksHubBLE` object is CLI-shaped: it prints stdout to the terminal by default, writes tqdm progress bars during download, auto-saves `PB_OF:`-marked output to server files, and has no reconnect-on-one-object API. Server code must not depend on those behaviors or on bleak directly.

**Decision:** One narrow seam — the abstract `Transport` class (`src/brick_console/transport.py`, 8 async operations: discover, connect, install-and-start, stop, write-stdin, subscribe-stdout, subscribe-status, disconnect). All server components (state machine, WS gateway, REST) depend on this interface only. The concrete implementation uses pybricksdev as a library where it fits (scan, connect handshake, RAM download+start, chunking — all verified exposed at library level, see `docs/research/pybricksdev-api-notes.md`) and bleak directly where pybricksdev doesn't fit (connection-level events, raw characteristic control if ever needed).

**Consequences:** BLE vendors are mockable in tests (a fake `Transport` drives the state machine); swapping the wire (e.g. D4's AppData option) or the library touches one module. Costs: a thin adapter layer and one indirection hop. pybricksdev behaviors that are wrong for a server (terminal printing, `PB_OF:` file writes, RxPY observables) are contained inside the adapter, translated to plain callbacks.

*Amended 2026-09-25 (docs-review finding 1):* the surface gained an 8th operation, `subscribe_status` — program lifecycle is not observable through stdout (a program may end without printing), so the state machine needs the hub's `STATUS_REPORT` snapshots (`USER_PROGRAM_RUNNING` flag edges are the only reliable program-end signal). Status flags cross the seam as `StatusFlags` (`IntFlag` mirroring pybricksdev's `StatusFlag` values); consumers derive edges from snapshots.

## D7 — Telemetry wire schema: JSON lines over hub stdout; raw-log-primary fan-out

**Status:** accepted (2026-09-25, issue #3)

**Context:** The dashboard's entire data plane rides hub stdout (D4): the hub-side `brick_telemetry` agent prints JSON lines, the server parses them into typed events, the ring buffer stores them (#6), and the WS gateway ships them to browsers (#8). Five things forced explicit decisions: (a) the hub has no wall clock, so someone must stamp receipt time; (b) stdout is push-only and a line can split across BLE notification chunks (transport.py `StdoutListener`), so framing and buffering are server-side; (c) stdout is a *shared* pipe — agent JSON, crash tracebacks, and (in M2+) arbitrary program output all share it, so a telemetry-only parser would silently drop the others; (d) Pybricks v4 exposes more measurable values than the docs' three example lines show — and two of the assumed ones are wrong (battery percent doesn't exist in the API at all, it must be derived from voltage; motor `load()` returns mNm, not a percentage) — so the field dictionary needed pinning against the v4.0.1 source; (e) three different things were called "event" across the docs (telemetry lines, WS messages, GATT status reports), and the WS envelope example reused the `"t"` key with a second meaning.

**Decision:**

*The wire.* Telemetry is UTF-8 JSON lines over hub stdout, one JSON object per `print()`, **CRLF-terminated** (MicroPython's line ending; the parser tolerates bare LF and strips a stray trailing `\r`). Compact JSON (separators `,`/`:`). A line may split at arbitrary byte boundaries across stdout notification chunks; the parser buffers partials with a hard cap of **4,096 bytes** (typical lines ≤ 200 B; worst-case aggregate ≈ 70 lines/s ≈ 5–6 KB/s — 1 imu + up to 6 port lines at 10 Hz + battery at 1 Hz). The physical channel on Pybricks v4.0.1 (profile ≥ 1.3) is the Pybricks command/event characteristic (`c5f50002`), carrying `WRITE_STDOUT` events — not the Nordic UART Service (NUS is the legacy pre-1.3 channel).

*The event kinds.* Four: `hub_info` (once per connect — a **snapshot event**: the server caches the latest one outside the ring buffer; #8 sends the cached snapshot before live stream and replay on client join), `battery` (~1 Hz), `imu` (~10 Hz), `port` (~10 Hz; one line per attached device, emitted **unconditionally per cycle**; `dev:"none"` only on a detach transition). The kind key is `"t"` — short wire keys throughout (`"t"`, `"v"`, `"c"`, `"pct"`…); the Python side uses full-word names (`voltage_mv`, `current_ma`, `percent`).

*The field dictionary + units.* `hub_info`: `name` (BLE name), `fw` (firmware string, e.g. "4.0.1"), `model` (literal `"technichub"`). `battery`: `v` mV, `c` mA, `pct` 0–100 — **derived hub-side** from `v` via a 2S Li-ion discharge curve, approximate under load, raw mV/mA always present. `imu`: `ax/ay/az` **mm/s²** (TechnicHub default mounting: +Z top, +X front; ≈ +9810 up at rest), `gx/gy/gz` **°/s** (right-hand rule), `up` side string (top/bottom/left/right/front/back). `port`: `p` (A–F), `dev`, then per-device fields — Motor `angle` ° (output shaft), `speed` °/s (100 ms window), `load` **mNm** (torque estimate, not %); ColorSensor `refl` %, `amb` %, `h`/`s`/`v` (h 0–360°, s/v %), `col` name or `"none"`; ForceSensor `f` N (~0–10), `d` mm (~0–8), `pressed` bool (3 N threshold); UltrasonicSensor `d` mm (2000 = no echo), `pr` bool; ColorDistanceSensor `d` **%** (BOOST-era unit trap), plus refl/amb/HSV/col; TiltSensor `pitch`/`roll` °; InfraredSensor `d` % relative; `none` = empty port. Device strings form an **open set**: `Motor`, `ColorSensor`, `ForceSensor`, `UltrasonicSensor`, `ColorDistanceSensor`, `TiltSensor`, `InfraredSensor`, `none` — the parser keeps `dev` a plain passthrough string, never a closed enum.

*Timestamps and ordering.* No wire timestamp — the hub has no wall clock. The parser stamps `received_at` (float UNIX seconds, via an **injectable clock**, default `time.time`) at successful decode. **No sequence number on the wire:** events are strictly ordered per BLE connection, and the ring buffer (#6) owns replay indices; a parser instance is per-connection and resets on reconnect.

*Unknown and malformed.* Malformed line (bad JSON/UTF-8, oversized partial): counted (`malformed_count`) and skipped — never raises, never kills the stream; subsequent lines must still parse. Unknown kind: wrapped as `UnknownEvent` carrying the parsed object verbatim. Unknown fields on known kinds: ignored. Unknown `dev` strings: passed through. Empty/whitespace-only lines: skipped silently, uncounted. A partial line in the buffer at disconnect is dropped and counted, never emitted half.

*Raw-log-primary fan-out.* The line splitter feeds **two** consumers: (1) the **raw-line log path — primary**: every raw line (JSON, traceback, program print) is retained — with its own retention bound downstream (#6) — and surfaced verbatim (server log +, in M2, the console pane), remaining re-parseable later; (2) the telemetry parser attached on top, which parses what it can and counts the rest. **Nothing the hub prints is ever dropped.**

*Envelope key naming.* The WS envelope's kind key is `"type"` (`telemetry`/`log`/`state`/…; architecture §3.2). Inside `data`, **`"t"` is reserved exclusively for the telemetry event kind**. Log-envelope data must never use `"t"` — suggested shape `{"src": "stdout", "line": "…"}`. (#8 implements this; D7 supersedes architecture §3.2's colliding log example.)

*Terminology.* **Telemetry event** = a hub-emitted JSON line. **Envelope event** = a WS message to browsers. **Status event** = a GATT STATUS_REPORT snapshot (transport `StatusFlags`). **Hub mode** = agent/program; **server state** = OFFLINE/ADVERTISING/AGENT/PROGRAM (architecture §4). Use these terms in docstrings and names; issues #4/#6/#8 quote them.

**Consequences:** The ring buffer (#6) stores parsed events plus raw lines under separate retention bounds; replay ordering is per-connection with the parser instance as the reset boundary, and `hub_info` requires the snapshot cache outside the event ring. The WS gateway (#8) implements the `"type"`/`data.t` rule and the snapshot-first join sequence. The BLE manager (#4) wires `subscribe_stdout` → line splitter → parser → ring/broadcast and owns per-connection parser construction. The hub-side agent (later issue) emits the canonical lines: CRLF, compact JSON, cadence per kind, `pct` computed hub-side, `dev` strings from the open set. The fake-hub/sim tier (testing.md) simulates: CRLF-terminated canonical lines split at arbitrary chunk boundaries (including mid-CRLF), all four kinds at cadence, occasional malformed lines, occasional unknown kinds. Forward compatibility is versioned by addition: new kinds and new fields on known kinds are safely ignorable by old servers. If Q4's throughput measurement disappoints, the AppData wire swap (D4) is contained to `brick_telemetry` + the parser input — the event model, ring buffer, and WS envelope are untouched. If multi-hub (R10) ever lands, a per-hub namespace rides the envelope's `hub` field, not the telemetry line.
