## D1 — Telemetry comes from hub-side code, not REPL polling

**Status:** accepted (2026-09-23, reaffirmed 2026-09-24)

**Context:** The dashboard needs live battery/IMU/port/motor/sensor data. Two candidate channels exist: (a) poll the Pybricks REPL over BLE, or (b) run a program on the hub that pushes data.

**Decision:** Hub-side code. The Pybricks BLE profile exposes no ambient telemetry — only static device info, capabilities, and 3 battery *warning flags*; the hub stdio pipe (Pybricks event characteristic on v4.0.1; the Nordic UART Service is the legacy pre-1.3 channel) carries `print()` output and nothing else. Rich data therefore requires a program. REPL polling is rejected because it interrupts whatever program is running (confirmed by pybricks-hub-tester's own caveat).

**Consequences:** Telemetry ships as an importable MicroPython library (`brick_telemetry`, see architecture §2.2) plus a thin agent wrapper the server installs in hub RAM when idle. User programs can opt in by importing the library, keeping the dashboard live in Program mode. The wrapper never occupies the 5 permanent slots.
