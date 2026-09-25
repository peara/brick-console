## D6 — All hub access goes through the `Transport` interface

**Status:** accepted (2026-09-24 — resolves BRD Q1)

**Context:** The server needs BLE (scan, connect, install+run, stop, stdin/stdout) and pybricksdev 2.3.2 already implements the Pybricks GATT protocol — but its `PybricksHubBLE` object is CLI-shaped: it prints stdout to the terminal by default, writes tqdm progress bars during download, auto-saves `PB_OF:`-marked output to server files, and has no reconnect-on-one-object API. Server code must not depend on those behaviors or on bleak directly.

**Decision:** One narrow seam — the abstract `Transport` class (`src/brick_console/transport.py`, 8 async operations: discover, connect, install-and-start, stop, write-stdin, subscribe-stdout, subscribe-status, disconnect). All server components (state machine, WS gateway, REST) depend on this interface only. The concrete implementation uses pybricksdev as a library where it fits (scan, connect handshake, RAM download+start, chunking — all verified exposed at library level, see `docs/research/pybricksdev-api-notes.md`) and bleak directly where pybricksdev doesn't fit (connection-level events, raw characteristic control if ever needed).

**Consequences:** BLE vendors are mockable in tests (a fake `Transport` drives the state machine); swapping the wire (e.g. D4's AppData option) or the library touches one module. Costs: a thin adapter layer and one indirection hop. pybricksdev behaviors that are wrong for a server (terminal printing, `PB_OF:` file writes, RxPY observables) are contained inside the adapter, translated to plain callbacks.

*Amended 2026-09-25 (docs-review finding 1):* the surface gained an 8th operation, `subscribe_status` — program lifecycle is not observable through stdout (a program may end without printing), so the state machine needs the hub's `STATUS_REPORT` snapshots (`USER_PROGRAM_RUNNING` flag edges are the only reliable program-end signal). Status flags cross the seam as `StatusFlags` (`IntFlag` mirroring pybricksdev's `StatusFlag` values); consumers derive edges from snapshots.
