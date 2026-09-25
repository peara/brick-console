## D4 — v1 telemetry wire: JSON lines over hub stdout

**Status:** accepted (2026-09-23); measurement follow-up promoted to BRD Q4

**Context:** Two candidate wires for agent telemetry: (a) JSON lines via `print()` over hub stdout, or (b) binary packing via the Pybricks `AppData` module (lego-control-center's approach). On our firmware (Pybricks v4.0.1, profile ≥ 1.3) hub stdout travels as `WRITE_STDOUT` events on the Pybricks command/event characteristic (`c5f50002`) — the Nordic UART Service is the legacy pre-1.3 stdio channel, unused here.

**Decision:** Start with (a) — the pattern of the official Pybricks pc-communication tutorial. Debuggable from any REPL client, trivially parseable, no GATT plumbing beyond the Pybricks characteristic notifications.

**Consequences:** Potential throughput overhead vs AppData (JSON is verbose; ~10 Hz × few lines is well within BLE capacity). Q4 in the BRD tracks a measurement during M1; if it disappoints, swap to AppData behind the same server-side event types — the change is contained to `brick_telemetry` + the BLE manager parser.
