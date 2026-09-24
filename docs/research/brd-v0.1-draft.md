# Hub Dashboard — BRD (v0.1, draft for review)

**Project:** Self-hosted LEGO 51515 hub manager ("hubdock" — working title)
**Context doc:** `investigation.md` (tooling research + dashboard-replacement addendum, same folder)
**Status:** Draft — awaiting decisions on D1–D4 and open questions Q1–Q3 before Milestone 0.

---

## 1. Vision

The official LEGO Mindstorms Robot Inventor app retires **2026-10-01**. Replace its *management* capabilities with a self-hosted web app running on the Linux gateway box: the box holds the only BLE connection to the hub, and any browser on the LAN (primarily the Windows laptop) gets a full dashboard — live hub state, program install/run with console, and later remote control — without any BLE capability of its own.

The key architectural bet (confirmed by research): **BLE lives in the server, not the browser.** This is what upgrades the box from a "one-shot flasher" into an always-on hub gateway: continuous telemetry, a persistent command channel, and automatic recovery on hub power-cycles. No existing open-source project does this for the 51515 (see investigation.md addendum); nearest references are `lego-control-center` (browser-side, rich UX) and `brickrail` (server-side BLE + websocket GUI, architecture proof).

## 2. Goals and non-goals

**Goals**
1. Full-fidelity replacement of the LEGO app's *hub management* surface: dashboard, program install/run, console, program library.
2. Zero-BLE laptop experience — works from any browser on LAN/Tailscale.
3. Always-on: server auto-discovers the hub on power-up, reconnects unattended.
4. Extensible data plane: a second client (the M5Stack CoreInk e-ink panel) joins later without redesign.

**Non-goals (v1)**
- Word-blocks coding canvas (Pybricks Code already has it; we serve Python only).
- Firmware flashing via the web UI (one-time USB DFU step stays manual — rare event).
- Multi-user accounts / auth (single-user hobby; bind to LAN + Tailscale only).
- Support for hubs other than the 51515/PrimeHub class (design doesn't preclude it).

## 3. Users

One user (you), two personas in effect:
- **Builder at the laptop:** writes/runs programs, watches telemetry, controls the robot remotely.
- **Robot at the desk:** hub powered on whenever; expects the dashboard to just be there — no pairing rituals, no "open the app and connect".

## 4. Concept of operations — the two-mode state model

Pybricks runs **one user program at a time**. Rich monitoring therefore cannot coexist with running *your* program; the product is organized around an explicit mode switch the UI always reflects:

```
                     power on
   [HUB OFF] ─────────────────► [ADVERTISING]
        ▲                             │ server auto-connects
        │ power off                   ▼
   [HUB OFF] ◄─── any disconnect [CONNECTED: AGENT MODE]
                                       │ user clicks Run
                                       ▼
                              [CONNECTED: PROGRAM MODE]
                                (agent stopped; console live)
                                       │ Stop / program ends
                                       ▼
                              back to [AGENT MODE]
```

- **Agent mode (idle):** server has installed and started a small telemetry *agent* — a thin wrapper around the `hubdock_telemetry` library — in hub RAM (does not occupy the 5 permanent slots). Agent pushes: hub info (once), battery (~1 Hz), IMU + port/sensor/motor states (~10 Hz) as JSON lines over stdout. Dashboard is fully live.
- **Program mode:** user program installed and started; agent wrapper is gone. Dashboard shows console + "program running" state, and keeps full telemetry **only if the user program imports `hubdock_telemetry`** (opt-in); otherwise values freeze and are clearly marked stale.
- Mode transitions are server-initiated, one click each way. REPL-based port polling is deliberately avoided — it interrupts running programs (per pybricks-hub-tester's caveat, investigation.md [35]).

This mirrors what the LEGO app actually did (dashboard when idle; console when running) — but made explicit, because on Pybricks it's a hard constraint, not a UI choice.

## 5. User flows

**F0 — One-time bring-up (manual, ~30 min, once per hub)** — ✅ **DONE 2026-09-24**

Completed state:
- Hub flashed with Pybricks **v4.0.1** (stable) from `pybricks-primehub-v4.0.1.zip`; original LEGO firmware backed up to `~/hermes/m5stack/backups/lego-original-inventor-hub.bin` (1,015,808 B, md5 `d0c76999…`) — restore path: DFU mode + `pybricksdev dfu restore`.
- Env: `~/hermes/m5stack/hubdock/` — uv project, Python 3.12, pybricksdev 2.3.2 + bleak 3.0.2; `dfu-util` installed; udev rules in place.
- Hub advertises as **"Pybricks Hub"** at `38:D3:4E:D4:E6:A1`, RSSI −45 dBm at the box, Pybricks service UUID `c5f50001-…`.
- Hello-world installed + run over BLE from the box: stdout returned (battery 8085 mV). **F0 exit criterion met.** Remaining from original F0: Q3 long-session soak (folded into M1 usage).

1. Put hub in USB DFU mode (hold Bluetooth button + USB cable); flash Pybricks stable from the Linux box CLI (`pybricksdev flash usb` or via code.pybricks.com from any Chromium).
2. On server: `rfkill unblock bluetooth`, install `pybricksdev` + `bleak`, udev rules if needed.
3. Hub powers on → verified visible in `pybricksdev` BLE scan. Escape hatch documented: DFU re-flash back to LEGO firmware any time.

**F1 — Open dashboard (daily path)**
1. Hub powered on anywhere in BLE range of the box.
2. Server auto-connects within seconds, installs + starts agent (RAM), telemetry flows.
3. User opens `http://<box>:<port>` on the laptop → live cards: battery %, port map with detected devices, live sensor/motor values, IMU orientation, connection status chip (AGENT / PROGRAM / OFFLINE).

**F2 — Write & run a program**
1. Editor pane (file browser over server-side program library, plain `.py`, syntax highlight; Monaco later).
2. **Run** → server stops agent → compiles + downloads to hub RAM → starts program → stdout/stderr streams into console pane.
3. **Stop** → server sends stop → agent re-installed → dashboard returns to live mode.
4. Crashes show traceback in console; hub stays connected; Run again is one click.

**F3 — Remote control session**
1. Dashboard "Control" tab (on-screen D-pad/sliders; gamepad API later).
2. Commands travel browser → WS → server → stdin to the *user's* control program on the hub (official `pc-communication` pattern, investigation.md [22]). Last-one-wins semantics, throttled to what BLE handles (~20–50 ms/write).
3. End session → back to agent mode.

**F4 — Program library management**
1. Programs saved on server filesystem (`programs/*.py`), listed in UI with metadata.
2. One-click re-run of any saved program (same path as F2 step 2).
3. *(Stretch — see Q2)* write to the hub's 5 permanent slots + slot picker, so programs can start button-only without the server.

**F5 — CoreInk e-ink panel (later, restores the original project goal)**
1. CoreInk (Arduino, Wi-Fi) subscribes to the server's telemetry (MQTT or simple HTTP poll).
2. Displays battery, port summary, active program; e-ink discipline: ≥15 s refresh interval.

**F6 — Conflict handling (the flow people forget)**
1. User opens official Pybricks Code IDE while server is connected → BLE allows one central → server gets disconnected.
2. Server backs off politely (retry scan every ~10 s), UI shows "external client took the hub", reconnects when free. No fighting over the hub.

## 6. Functional requirements (MoSCoW)

| # | Requirement | Prio |
|---|---|---|
| R1 | Auto-discover + auto-connect known hubs on power-up; status visible; auto-reconnect after power-cycle or external-client release | Must |
| R2 | Agent-mode dashboard: battery, port map + device types, live motor/sensor values, IMU | Must |
| R3 | Browser editor + install/run/stop over BLE + live stdout/stderr console | Must |
| R4 | Server-side program library (CRUD files) with one-click run | Must |
| R5 | Two-mode state model enforced in UI; frozen values marked stale unless the running program opted into `hubdock_telemetry` | Must |
| R6 | On-screen remote-control panel → stdin command channel | Should |
| R7 | Telemetry ring buffer + simple graphs (battery over time, sensor traces) | Should |
| R8 | Control via Gamepad API in browser (Xbox controller on laptop) | Could |
| R9 | Hub slot management (write to permanent slots, slot picker) | Could (blocked on Q2) |
| R10 | Multi-hub support (server holds N connections; Brickrail data point: ~7–8/adapter) | Could |
| R11 | CoreInk e-ink status client | Could (M4) |
| R12 | Data-log export (CSV of telemetry buffer) | Could |
| R13 | Firmware flash via web UI | Won't (v1) |
| R14 | Word-blocks coding | Won't (v1) |
| R15 | Multi-user/auth | Won't (v1) |

## 7. Non-functional requirements

- **Latency:** telemetry end-to-end ≤ 250 ms at 10 Hz; control command ≤ 150 ms.
- **Range:** hub within ~10 m of the box's adapter (reposition adapter if marginal).
- **Availability:** hobby-grade. Server restart OK; stateless reconnect is a hard requirement (R1). No DB in v1 — files + in-memory buffers.
- **Security:** bind to LAN/Tailscale interface only; never public. Optional shared token if ever exposed beyond Tailscale.
- **Capacity:** 1 hub by design; ≤ 4 concurrent hubs realistic on one adapter.
- **Observability:** server logs all BLE events (connect/disconnect/mode switches); UI shows session history.

## 8. Architecture overview

```
┌─────────────┐  WebSocket (JSON events)  ┌──────────────────────────────┐
│  Browser UI │◄─────────────────────────►│  hubdock server (Linux box) │
│ (laptop,    │                           │  ├ FastAPI app + WS gateway  │
│  any LAN    │                           │  ├ BLE manager (bleak /      │
│  device)    │                           │  │  pybricksdev as library)  │
└─────────────┘                           │  ├ Program library (files)   │
                                          │  └ Telemetry buffer (ring)    │
┌──────────────┐   MQTT/poll (later)      └──────────┬───────────────────┘
│ CoreInk e-ink│◄─────────────────────────────────────┘
└──────────────┘                                   BLE (GATT: NUS + Pybricks chars)
                                          ┌──────────▼───────────┐
                                          │ 51515 hub (Pybricks) │
                                          │  agent / user prog   │
                                          └──────────────────────┘
```

**Hub↔server wire protocol (v1):** JSON-lines over the Nordic UART Service (stdout) for telemetry, stdin writes for commands, Pybricks GATT commands for install/run/stop (documented profile, investigation.md [38]). The server impersonates the role Pybricks Code plays in a browser.

**Data model (v1):** `Program` (name, code, mtime); `TelemetryEvent` (typed JSON: hub_info | battery | imu | port | motor | sensor | log); `Session` (connection + mode history). Files on disk + memory only.

## 9. Key design decisions (for review — D1/D4 need your sign-off)

- **D1 — Telemetry comes from hub-side code (agent/library), not REPL polling.** The Pybricks BLE profile exposes no ambient telemetry: only static device info, capabilities, and 3 status *warnings*; the NUS stdio pipe carries `print()` output and nothing else. Rich dashboard data therefore requires hub-side code — shipped as an **importable library** (`hubdock_telemetry`) plus a thin default wrapper program the server installs in RAM when idle. REPL polling rejected (interrupts programs). Consequences: (a) dashboard during *your* program degrades **only if you opt out** — importing the library keeps full telemetry; (b) the wrapper never occupies the 5 permanent slots. *This is the single biggest product decision.*
- **D2 — Server-side BLE only.** No Web Bluetooth anywhere. Enables zero-BLE laptop + always-on monitoring; costs us a custom UI instead of forking pybricks-code/lego-control-center wholesale (their UX is the reference).
- **D3 — Python-first, no blocks.** Blocks = huge scope; Pybricks Code covers that need if it ever arises.
- **D4 — v1 telemetry over stdout JSON-lines** (pattern proven by the official pc-communication tutorial [22]), **not** `AppData` (used by lego-control-center [34]). Stdin+stdout is simpler, debuggable from a laptop REPL too; AppData can replace it later without UI change. *Needs your sign-off.*
- **D5 — Programs live on the server**, hub slots optional (Q2). Server is the source of truth; hub RAM is ephemeral.

## 10. Open questions (verify in M0/M1)

- **Q1:** Does `pybricksdev` (as a library) expose everything the flows need: BLE scan-by-name, download+run, stop, stdin/stdout streams? (CLI does; library API is claimed to mirror it — confirm surface, investigation.md [6][37].)
- **Q2:** Can a program be written to one of the hub's 5 permanent *slots* programmatically, or only run-to-RAM? (pybricks-code does save-to-slot on run; CLI equivalent unverified. R9 blocked on this.)
- **Q3:** Is the box's USB BLE adapter (hci0, currently rfkill-blocked) reliable for long-lived connections? (Verify during M0: connect, soak for an hour, watch for BlueZ disconnects. Cheap dongles are notoriously flaky; a BT 5.0 adapter is a known-good upgrade if needed.)

## 11. Milestones

| M | Scope | Exit criteria |
|---|---|---|
| **M0 Bring-up** | Unblock BLE, deps, flash hub, CLI hello-world run from the box over BLE | A program installed+run via `pybricksdev` from the Linux box; hub telemetry seen; Q3 soak started |
| **M1 Read-only dashboard** | BLE manager + agent program + WS telemetry + minimal web page | From the laptop: live battery/ports/sensor values ≤ 5 s after hub power-on |
| **M2 Run & console** | Editor pane + install/run/stop + live console | F2 works end-to-end; crash → traceback visible; one-click back to agent mode |
| **M3 Control & library** | On-screen remote, stdin channel, program library CRUD | F3 + F4 working; last-one-wins verified under rapid clicking |
| **M4 CoreInk panel** | MQTT/publish telemetry; CoreInk Arduino client | E-ink shows hub state; ≥15 s refresh respected |
| **M5 Polish** | Graphs, gamepad, slots (if Q2), CSV export | Nice-to-haves from §6 |

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| One central at a time — external IDE steals hub | Dashboard drops | F6 back-off protocol; document "only one manager" |
| BLE adapter flakiness on long sessions | Reconnect loops | Q3 soak in M0; upgrade adapter (BT 5.0, external antenna) if needed |
| Program mode blinds the dashboard | User confusion | D1: explicit mode chips; agent auto-restore on Stop |
| pybricksdev library gaps (Q1) | Some flows need custom GATT code | Protocol is documented ([38]); bleak fallback is a few hundred lines |
| Pybricks 4 churn (was "beta"; v4.0.1 is now stable — flashed 2026-09-24) | API drift | Pin v4.0.1; upgrade deliberately after checking changelog |
| Hub out of BLE range from box | No data | Physics; move adapter/hub; report RSSI in dashboard |

## 13. References

- `investigation.md` — tooling research (paths, BLE protocol, CoreInk constraints) and dashboard-replacement addendum. Source numbers cited above refer to its Sources block.
- Key projects: `thomasbrus/lego-control-center` [33] (UX reference, agent pattern [34]); `aztechell/pybricks-hub-tester` [35] (port-scan UX, REPL caveat); `Novakasa/brickrail` [36] (architecture proof); `pybricks/pybricksdev` [6] + BLE profile [38] (wire protocol); official pc-communication tutorial [22] (stdout/stdin pattern).