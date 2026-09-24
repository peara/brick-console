# Architecture — brick-console

Companion to [brd.md](brd.md). Covers components, protocols, the two-mode state model, and the data plane.

## 1. System context

```mermaid
flowchart LR
    Browser["Any browser<br/>(laptop / phone)"]
    CoreInk["CoreInk e-ink panel (M4)"]

    subgraph Server["brick-console server (Linux gateway box)"]
        WebApp["web app (FastAPI)<br/>WS event gateway<br/>telemetry ring buffer<br/>program library (fs)"]
        BLE["BLE manager service<br/>(bleak / pybricksdev as library)"]
    end

    Hub["51515 Inventor Hub<br/>Pybricks v4.0.1 firmware<br/>running: agent | user prog"]

    Browser <-->|"HTTP + WebSocket (LAN / Tailscale)"| Server
    CoreInk <-->|"MQTT / HTTP poll"| Server
    BLE <-->|"BLE GATT (Pybricks service + Nordic UART)"| Hub
```

## 2. Components (v1)

### 2.1 Server (Linux box)

| Component | Responsibility | Tech (planned) |
|---|---|---|
| **Web app** | Static dashboard + editor UI, REST endpoints, auth token check | FastAPI + uvicorn |
| **WS gateway** | Push telemetry/log events to browsers; receive control commands | FastAPI WebSocket |
| **BLE manager** | Hub discovery, connection lifecycle, two-mode state machine, stdin/stdout pipes, run/stop | asyncio + bleak 3.x (+ pybricksdev internals where convenient) |
| **Telemetry store** | Ring buffer (bounded deque) of parsed events; replay to late-joining clients | in-memory, collections.deque |
| **Program library** | `programs/*.py` on server fs, CRUD via REST | plain files |

### 2.2 Hub side (`brick_telemetry`)

A single importable MicroPython module distributed two ways:
- **Agent wrapper** — a ~10-line program the server installs in RAM when idle (M1). It imports `brick_telemetry` and serves the idle dashboard.
- **User programs** — `import brick_telemetry` (opt-in) to keep the dashboard live while your program runs (M2+).

Library responsibilities: discover devices on ports (idempotent), read battery/IMU/motors/sensors on a tiered cadence, emit JSON lines to stdout, consume stdin commands (later: control).

Tiered cadence (from research — lego-control-center pattern):
- once on connect: `hub_info` (name, firmware, hub type)
- ~1 Hz: `battery` (voltage, current, %)
- ~10 Hz: `imu` (accel, gyro, orientation), `motor`/`sensor` states per port

### 2.3 Browser UI

Single-page app, no framework (vanilla + WebSocket client). Views:
- **Dashboard** — status chip (AGENT/PROGRAM/OFFLINE/EXTERNAL), battery card, port map with device chips, live values, IMU orientation widget
- **Console** — program editor (textarea→Monaco later) + stdout/stderr pane + Run/Stop
- **Control** (M3) — D-pad/sliders → stdin commands
- **Library** (M3) — program list, open/save/re-run

## 3. Wire protocols

### 3.1 Hub ↔ server (BLE GATT)

The Pybricks firmware exposes (per [pybricks-ble-profile](https://github.com/pybricks/technical-info/blob/master/pybricks-ble-profile.md)):

| Characteristic | Use in brick-console |
|---|---|
| **Pybricks Command/Event char** (`c5f50002-…`) | install (WRITE_USER_PROGRAM_META → WRITE_USER_RAM chunks) + START/STOP_USER_PROGRAM; stdin writes (cmd 6) |
| **Hub Capabilities char** (`c5f50003-…`) | max write size → chunk sizing |
| **Nordic UART Service** (Rx notify / Tx write) | stdout/stderr stream from hub; REPL channel (unused in v1) |
| Device Information Service | firmware version display |

Telemetry framing: **JSON lines over NUS stdout** (decision D4). One JSON object per `print()`. E.g.:
```json
{"t": "battery", "v": 8085, "c": 42, "pct": 87}
{"t": "imu", "ax": 120, "ay": -980, "az": 0, "gx": 0, "gy": 0, "gz": 3}
{"t": "port", "p": "A", "dev": "Motor", "angle": 0, "speed": 0, "load": 0}
```

### 3.2 Browser ↔ server (WebSocket)

Server→browser event envelope:
```json
{"type": "telemetry", "hub": "Pybricks Hub", "data": {"t": "battery", ...}}
{"type": "log", "hub": "Pybricks Hub", "data": {"t": "stdout", "line": "..."}}
{"type": "state", "hub": "Pybricks Hub", "data": {"mode": "agent"}}
```

Browser→server commands: `{"type": "run", "program": "hello.py"}`, `{"type": "stop"}`, `{"type": "control", "btn": "fwd", "v": 80}` (M3), `{"type": "stdin", "line": "…"}`.

### 3.3 CoreInk ↔ server (M4)

Decision deferred to M4 (MQTT broker vs plain HTTP poll). Both ride the same telemetry ring buffer via a small publish shim. CoreInk constraints: no BLE under UiFlow2 (no PSRAM) → Arduino/NimBLE path for any direct-hub work, Wi-Fi client for the panel role.

## 4. State model (server-tracked)

```mermaid
stateDiagram-v2
    OFFLINE --> ADVERTISING: scan
    ADVERTISING --> AGENT: connect
    AGENT --> PROGRAM: run
    ADVERTISING --> OFFLINE: hub off
    AGENT --> OFFLINE: external client took hub - backoff
    PROGRAM --> AGENT: program ended / crashed / Stop
```

| State | Meaning | Dashboard shows |
|---|---|---|
| OFFLINE | Hub not seen; scanning loop running | "OFFLINE — scanning…" |
| ADVERTISING | Hub advertisement detected; connect attempt in flight | "Connecting…" |
| AGENT | Connected; telemetry agent installed and running | Live values |
| PROGRAM | User program owns the hub | Console live; values stale-marked unless `brick_telemetry` imported |

Rules:
1. One BLE central at a time — if the server loses the hub to an external client, back off (~10 s rescan), never fight.
2. Program end (normal, crash, or Stop) → server reinstalls agent automatically.
3. Hub power-cycle → full reconnect path with fresh agent install; total time budget ≤ 5 s.
4. All state transitions logged with timestamps (session history).

## 4.5 BLE manager service (M1 concrete plan)

Runs as a systemd user service (`brick-console.service`), auto-started, auto-restarted. Loop:

1. `BleakScanner.find_device_by_name("Pybricks Hub")` (config: name/address)
2. Connect → read capabilities → install agent wrapper → start → state=AGENT
3. Subscribe NUS notifications → parse JSON lines → ring buffer + WS broadcast
4. On disconnect: log reason, state=OFFLINE, rescan loop (immediate, then backoff)

## 5. Data retention

- Telemetry: bounded ring (default 5,000 events); CSV export endpoint reads it (R12).
- Program library: plain files under `programs/` — the only durable state.
- No DB in v1 (decision D5). If retention needs grow: SQLite at the server tier only.

## 6. Security posture

- Bind to LAN + Tailscale interface; never 0.0.0.0 exposure to the internet.
- Optional shared token header checked by FastAPI middleware (off by default, single-user LAN).
- The hub is unauthenticated by design (LEGO/Pybricks constraint) — acceptable because the hub is only reachable within BLE range of a physically secured box.

## 7. Repository layout (target)

```
brick-console/
├── docs/               # this doc set
│   ├── brd.md
│   ├── architecture.md
│   ├── decisions.md
│   └── research/       # investigation.md, brd-v0.1-draft.md (frozen archive)
├── programs/           # user program library (server fs)
├── firmware/           # firmware zips + metadata (gitignored except README)
├── src/brick_console/  # server package (FastAPI + BLE manager)
├── agent/brick_telemetry.py  # hub-side library
├── pyproject.toml
└── uv.lock
```

## 8. Technology choices (summary)

| Layer | Choice | Why |
|---|---|---|
| BLE | bleak 3.x primary; pybricksdev vendored GATT logic where it fits | bleak is the reference async BLE lib on Linux; pybricksdev proved the install/run flow at bring-up |
| Web | FastAPI + uvicorn | async-native (pairs with bleak), WS support, typed |
| Hub runtime | Pybricks v4.0.1 stable | flashed; reversible via DFU backup |
| Hub wire | NUS stdout JSON lines | simplest debuggable channel (D4) |
| Frontend | vanilla JS + WS | zero build tooling for M1; upgrade path to a framework if needed |

See [decisions.md](decisions.md) for rationale records.