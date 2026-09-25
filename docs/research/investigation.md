# M5Stack CoreInk + Mindstorms 51515 — Tooling Investigation

> **Frozen research archive (2026-09-23).** Superseded by [decisions.md](../decisions.md) wherever they conflict — especially the firmware version (now Pybricks v4.0.1 stable, D-FL, not v3.x) and BLE availability (adapter works; was rfkill-soft-blocked at time of writing). Statements below are historical, kept for context only.

Status display: M5Stack CoreInk (ESP32-PICO-D4, 1.54" 200×200 e-ink) driven by Mindstorms Robot Inventor 51515 / SPIKE Prime large hub, with a laptop in the loop.

## TL;DR

The stack is viable with three independent tool paths, all free. The core decision: **what firmware runs on the hub.**

- **Path A (recommended): Pybricks firmware on the hub.** Hub runs MicroPython programs standalone. Laptop connects over BLE with `pybricksdev` / bleak. CoreInk talks BLE GATT to the laptop or Wi-Fi/MQTT.
- **Path B: keep LEGO official firmware.** Laptop drives the hub directly over BLE with the documented LEGO Wireless Protocol v3 (LWP3) — `bleak` + `pybricksdev`'s message classes. CoreInk role unchanged.
- **Path C (wired, robust, no BLE): Pybricks 4 beta's `UARTDevice`** from hub port to CoreInk GPIO, exactly the pattern Anton's Mindstorms uses with LMS-ESP32 boards.

## The hardware

**M5Stack CoreInk** — ESP32-PICO-D4, 240MHz dual-core, 520KB SRAM, 4MB flash, 2.4GHz Wi-Fi + BLE, 200×200 1-bit 1.54" e-ink (GDEW0154M09, full refresh 0.82s, partial 0.24s), 390mAh battery, RTC, buzzer, 3 buttons, HY2.0-4P + M-Bus + HAT connectors. Toolchains: UiFlow2 (blockly/MicroPython), Arduino, PlatformIO, ESP-IDF.[13][20]

**LEGO 51515 hub** = SPIKE Prime large hub hardware, identical; Pybricks treats them as one class (`PrimeHub`/`InventorHub`).[2] The official Robot Inventor app is discontinued **2026-10-01**, no further updates — the community standard is Pybricks.[1] With official firmware, the hub speaks the LEGO Wireless Protocol v3 over a single BLE GATT characteristic (0x1624).[8]

## Path A — Pybricks on the hub (recommended)

Firmware: Pybricks (free, open source). Python coding is free; block coding is a paid supporter feature.[4] The 51515/Prime hub is fully supported.[2] Install from code.pybricks.com; you can restore official LEGO firmware at any time (DFU mode: hold Bluetooth button + USB).[3] The hub then runs MicroPython programs standalone, saved on the hub, started by the button press — no computer connection needed.[4][3]

Laptop ↔ hub: `pybricksdev` CLI — flash over USB, run scripts over BLE or USB (`pybricksdev run ble/usb script.py`), plus a `pybricksdev lwp3 repl` mode.[6] Linux needs udev rules (`pybricksdev udev | sudo tee ...`); Web Bluetooth in Chromium needs `chrome://flags/#enable-experimental-web-platform-features` enabled.[5] A Python bleak client can also chat with a running hub program bidirectionally (write stdout events / read stdin commands) — official tutorial code exists.[22]

CoreInk ↔ hub: two sub-options, covered below.

## Path B — Keep official LEGO firmware on the hub

LEGO open-sourced LWP3 (github.com/LEGO/lego-ble-wireless-protocol-docs): scan by service UUID, single characteristic 0x1624, binary messages for hub properties, port commands, motor commands, sensor notifications.[8] `pybricksdev` ships a complete Python LWP3 message parser/encoder (`pybricksdev.ble.lwp3`) usable standalone from the laptop, including an interactive REPL tool.[6][26]

Options on the laptop: raw bleak + LWP3 message classes from pybricksdev[6][26]; `spikeble` (runs MicroPython code on stock-firmware hubs over BLE RPC, needs SPIKE ≥ certain firmware);[11] `legoeducation` PyPI package (BLE RPC client for LEGO Education devices, bleak backend, custom transport pluggable).[12]

**Pybricks-vs-official caveat:** both firmwares can't be resident simultaneously; you re-flash to switch, but both are recoverable.[2][3]

## Path C — Wired: Pybricks 4 `UARTDevice` (hidden gem)

Pybricks 4 (currently beta, v4.1.0b1 docs) rebuilt the port stack: PUP ports can leave "LEGO mode" and become raw UART.[21][23] `UARTDevice(port, baudrate=115200, power_pin=2)` — pins 5/6 are TX/RX (3.3V), pins 1/2 battery power for the peripheral.[21] Wire hub port → CoreInk G25/G26 (or any free GPIO pair) with common ground. This is exactly Anton's Mindstorms' LMS-ESP32 architecture; their `uRemote` library is a ready RPC layer (hub client, ESP32 server, 115200 baud).[24][23] Note standard Pybricks firmware may need a patched/advanced build for UARTDevice — check current builds at code.pybricks.com.[24]

## CoreInk toolchain options

- **UIFlow2 (MicroPython):** flash via M5Burner, program via web IDE or USB terminal.[20] **Caveat: CoreInk has no PSRAM** — the old UiFlow1 BLE blocks were PSRAM-only, and CoreInk's UiFlow2 board manifest pulls only `startup/hat/unit/chain` libs — not the `m5ble`/`bleuart` packages that full-featured boards get.[14][16] So under UiFlow2, CoreInk's BLE support is uncertain/likely absent — verify on hardware before betting on it.
- **Arduino / PlatformIO:** BLE fully available on ESP32 — Qiita articles demonstrate CoreInk BLE server+client with standard Arduino BLE libs;[25] `Legoino` is an ESP32-first Arduino library speaking LWP3 directly (connect to Technic Hub, Control+ hub, motors/sensors; built on NimBLE-Arduino, up to 9 hubs).[9] Caveat: legoino's tested-hub list doesn't name the 51515 large hub — likely works, verify.
- **Wi-Fi is CoreInk's strength:** MQTT/HTTP to the laptop is trivial and avoids BLE central limitations entirely.

## Recommended setup (my pick)

1. **Hub:** Pybricks firmware (stable v3.x) — best docs, best tooling, free, reversible.[3][4]
2. **Laptop as broker:** Python bleak app talks BLE to hub (Pybricks channel) and Wi-Fi/MQTT to CoreInk. Laptop is the only device doing BLE central duty.
3. **CoreInk:** UIFlow2 or Arduino. If you want CoreInk↔hub direct BLE, Arduino + Legoino or NimBLE is the sure path on this no-PSRAM board.[9][25]
4. **E-ink discipline:** full refresh 0.82s and M5Stack says avoid high-frequency refresh — design the UI for 15s+ intervals, partial refresh (0.24s) for small updates.[13][20]

## Key hardware constraints to remember

- CoreInk BLE central under MicroPython is the risky unknown — CoreInk's UiFlow2 build likely omits BLE libs (no PSRAM, trimmed manifest) and UiFlow1 BLE blocks are PSRAM-only.[14][16]
- 51515 hub pairing: newer hubs require `pair=True` in LWP3Device (Pybricks 3.6+ for pairing support).[7]
- Pybricks 4 is beta — for stability start with stable v3.x; `power_pin` exists in 4.x only.[21][23]
- LEGO official firmware updates can break third-party tools (e.g. SPIKE firmware v1.8.149 broke `import app` in spikeble).[11]

## Dashboard-replacement research (2026-09-23, addendum)

### Existing projects found

| Project | What it is | Fit |
|---|---|---|
| **thomasbrus/lego-control-center** — 2★, TypeScript, MIT-style site at lego-control-center.netlify.app[33][34] | The closest thing to a "hub dashboard replacement": multi-hub connect, LED control, realtime IMU, battery %, shutdown, port detection, motor status (speed/load/angle), live sensor values. Web Bluetooth (browser-side BLE). | Feature-wise nearly exactly what the app's dashboard did — but browser-side BLE means the Windows laptop browser talks to the hub directly; the Linux box is not in the loop. Great UI/UX reference and possible fork base. |
| **aztechell/pybricks-hub-tester** — 1★, JavaScript, static GitHub Pages app[35] | Port-scanning dashboard: hub model/firmware/battery/IMU readouts, per-port device detection + live values, motor DC sweep test with RPM/current charts. Uses the Pybricks REPL to scan ports. | Excellent "port dashboard" reference (what LEGO's app shows on connect). REPL-based port scan interrupts running program. |
| **Novakasa/brickrail** — 114★, MIT[36] | Train automation: Python **server handles BLE** to hubs + Godot GUI talks to it over websockets. Proves the exact architecture (server-side BLE + websocket GUI). SPIKE Prime hub listed as compatible (less tested). | Architecture proof, not a dashboard. Their ~7-8 simultaneous BLE connection limit per adapter is a useful data point. |
| **pybricks/pybricks-code** — official, MIT[39] | The web IDE itself. Self-hostable (`yarn install && yarn dev`), but it uses **Web Bluetooth** — BLE runs in the browser. | Self-hosting it on the Linux box only helps if the browser machine has BLE; for laptop control you'd still need the server-side bridge. |
| Gumphrie/bcc, 2ndClemens/51515-ble-remote, xpunsterx/poweredup-js, firestorm22/lego-spike-prime-web-controller | Various Web-Bluetooth remotes/controls for PU/SPIKE hubs (all browser-side BLE). | Confirm the pattern: everything browser-side, nothing server-side for 51515. |

**Conclusion: no existing project combines server-side BLE (Linux box) + web dashboard for the 51515.** The gap is real. The LEGO app's "manage everything" features all have proven community building blocks (Pybricks GATT profile for install/run[38], AppData-style hub-side telemetry agent[34], bleak/pybricksdev for server BLE), but nobody has assembled them into one self-hosted dashboard.

### Architecture for a self-hosted "hub manager" on the Linux box

The key constraint: **Web Bluetooth runs in the browser's machine** — so a laptop browser cannot use the Linux box's BLE radio. Two workable patterns:

1. **Server-side BLE (recommended):** FastAPI/Flask + `pybricksdev`/`bleak` backend on the Linux box, using its hci0 adapter (present, USB; currently rfkill-soft-blocked — unblock first: `rfkill unblock bluetooth`). WebSocket/SSE streams telemetry to any laptop browser on the LAN. This is the Brickrail-proven pattern[36] and lets the CoreInk (Wi-Fi/MQTT) join the same data plane.
2. **Browser-side BLE:** self-host lego-control-center or pybricks-hub-tester (both static apps) and accept that the browser machine needs its own BLE radio + Chrome/Edge. No server in the loop.

Web IDE piece: `pybricks-code` is MIT and self-hostable[39], but same browser-side BLE constraint applies; a server-side equivalent would wrap `pybricksdev run ble` / the documented download-and-start GATT procedure[38] behind a "Compile & Run" button.

### Install-code-over-web feasibility

- Program install over BLE is fully documented: Pybricks GATT profile defines `WRITE_USER_PROGRAM_META` (3), `WRITE_USER_RAM` (4), `START_USER_PROGRAM` (1) — download program into hub RAM, then start[38]. `pybricksdev` library API does exactly this (`pip install pybricksdev`)[6][37].
- Hub-side, the dashboard telemetry agent uses `AppData` module + STDIN commands, with different-frequency loops for hub info / battery / IMU / motors — proven pattern from lego-control-center[34].
- Firmware flashing itself needs USB DFU mode (hold Bluetooth button + plug in)[40] — one-time per hub; everything after that is BLE.

### Recommended MVP slice

1. Unblock BLE on the Linux box (`rfkill unblock bluetooth`) and flash Pybricks stable on the hub via the official web IDE once (Chromium, one-time).
2. Backend: FastAPI + bleak on the Linux box — scan, connect, install & run programs (wrapping `pybricksdev`), stream stdout/stderr over WebSocket.
3. Hub-side agent: standard Pybricks program reading battery/IMU/motor states, pushing via AppData/stdout at tiered intervals (pattern from [34]).
4. Frontend: laptop browser hits the FastAPI server on the LAN — dashboard + code editor + Run/Stop buttons. Optionally reuse lego-control-center's UI patterns (or fork it and swap its Web-Bluetooth layer for a WebSocket client).

## Sources

[1] https://play.google.com/store/apps/details?id=com.lego.retail.mindstorms&hl=en_US — LEGO MINDSTORMS Inventor app (Google Play)
[2] https://docs.pybricks.com/en/latest/hubs/primehub.html — Pybricks: Prime Hub / Inventor Hub docs
[3] https://pybricks.com/learn/getting-started/install-pybricks — Installing Pybricks
[4] https://pybricks.com — Pybricks homepage
[5] https://pybricks.com/project/pybricks-on-linux — Using Pybricks on Linux
[6] https://github.com/pybricks/pybricksdev — pybricksdev CLI
[7] https://docs.pybricks.com/en/stable/iodevices/lwp3device.html — Pybricks LWP3Device
[8] https://lego.github.io/lego-ble-wireless-protocol-docs — LEGO Wireless Protocol 3.0 docs
[9] https://github.com/corneliusmunz/legoino — Legoino (Arduino ESP32 LEGO library)
[11] https://github.com/MGross21/spikeble — spikeble
[12] https://pypi.org/project/legoeducation — legoeducation PyPI
[13] https://docs.m5stack.com/en/core/coreink — M5Stack CoreInk docs
[14] https://docs.m5stack.com/en/uiflow/blockly/advanced/ble — M5Stack UiFlow1 BLE blocks (PSRAM only)
[16] https://github.com/m5stack/uiflow-micropython/blob/2.0.3/m5stack/boards/M5STACK_CoreInk/manifest.py — CoreInk UiFlow2 board manifest
[20] https://docs.m5stack.com/en/uiflow2/m5coreink/program — CoreInk UiFlow2 flash guide
[21] https://docs.pybricks.com/en/latest/iodevices/uartdevice.html — Pybricks UARTDevice docs
[22] https://pybricks.com/projects/tutorials/wireless/hub-to-device/pc-communication — Pybricks: Hub to PC Communication (bleak)
[23] https://antonsmindstorms.com/2026/08/14/whats-new-in-pybricks-4 — Anton's Mindstorms: What's new in Pybricks 4
[24] https://docs.antonsmindstorms.com/en/latest/Software/uRemote/docs — uRemote UART RPC docs
[25] https://qiita.com/sushisushiBoo/items/19f01bab110a8bcbc783 — CoreInk BLE (Arduino) article (Japanese)
[26] https://docs.pybricks.com/projects/pybricksdev/en/stable/api/ble/lwp3/bytecodes.html — pybricksdev LWP3 bytecodes (HubKind)
[33] https://github.com/thomasbrus/lego-control-center — lego-control-center: Web UI for Pybricks hubs (multi-hub, IMU, battery, motors, sensors)
[34] https://github.com/orgs/pybricks/discussions/2553 — Pybricks discussion #2553: Control Center show-and-tell (AppData telemetry design)
[35] https://github.com/aztechell/pybricks-hub-tester — pybricks-hub-tester: Web Bluetooth dashboard with port scanning, live values, motor DC sweep
[36] https://github.com/Novakasa/brickrail — Brickrail: LEGO train automation — Python BLE server + Godot GUI over websockets
[37] https://github.com/pybricks/support/issues/284 — pybricksdev feature request #284: remotely starting a stored program (closed)
[38] https://github.com/pybricks/technical-info/blob/master/pybricks-ble-profile.md — Pybricks BLE profile: program download + run over GATT (commands 0-6, NUS)
[39] https://github.com/pybricks/pybricks-code — pybricks-code: official MIT-licensed web IDE, self-hostable (yarn dev)
[40] https://github.com/pybricks/pybricksdev/blob/master/README_dfu.rst — pybricksdev DFU README: firmware flashing via USB DFU mode
