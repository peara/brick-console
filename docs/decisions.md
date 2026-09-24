# Decision records — brick-console

Short ADR-style log. Each entry: context → decision → consequences. Numbers are stable IDs; superseded decisions stay for history.

## D1 — Telemetry comes from hub-side code, not REPL polling

**Status:** accepted (2026-09-23, reaffirmed 2026-09-24)

**Context:** The dashboard needs live battery/IMU/port/motor/sensor data. Two candidate channels exist: (a) poll the Pybricks REPL over BLE, or (b) run a program on the hub that pushes data.

**Decision:** Hub-side code. The Pybricks BLE profile exposes no ambient telemetry — only static device info, capabilities, and 3 battery *warning flags*; the Nordic UART stdio pipe carries `print()` output and nothing else. Rich data therefore requires a program. REPL polling is rejected because it interrupts whatever program is running (confirmed by pybricks-hub-tester's own caveat).

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

## D4 — v1 telemetry wire: JSON lines over NUS stdout

**Status:** accepted (2026-09-23); measurement follow-up promoted to BRD Q4

**Context:** Two candidate wires for agent telemetry: (a) JSON lines via `print()` over the Nordic UART stdio, or (b) binary packing via the Pybricks `AppData` module (lego-control-center's approach).

**Decision:** Start with (a) — the pattern of the official Pybricks pc-communication tutorial. Debuggable from any REPL client, trivially parseable, no GATT plumbing beyond NUS.

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

**Decision:** One narrow seam — the abstract `Transport` class (`src/brick_console/transport.py`, 7 async operations: discover, connect, install-and-start, stop, write-stdin, subscribe-stdout, disconnect). All server components (state machine, WS gateway, REST) depend on this interface only. The concrete implementation uses pybricksdev as a library where it fits (scan, connect handshake, RAM download+start, chunking — all verified exposed at library level, see `docs/research/pybricksdev-api-notes.md`) and bleak directly where pybricksdev doesn't fit (connection-level events, raw characteristic control if ever needed).

**Consequences:** BLE vendors are mockable in tests (a fake `Transport` drives the state machine); swapping the wire (e.g. D4's AppData option) or the library touches one module. Costs: a thin adapter layer and one indirection hop. pybricksdev behaviors that are wrong for a server (terminal printing, `PB_OF:` file writes, RxPY observables) are contained inside the adapter, translated to plain callbacks.
