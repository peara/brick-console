# Decision records — brick-console

Short ADR-style log. Each entry: context → decision → consequences. Numbers are stable IDs; superseded decisions stay for history.

Each ADR lives in its own file under [`docs/adr/`](adr/) — one file per decision, named `<ID>-<slug>.md`. New architecture-level decisions (wire protocol, state model, storage) get a new ADR file there **and a row in the table below**; do not append ADRs to this file. Statuses: `accepted` (in force), `superseded` (kept for history, links to successor), `amended` (original text kept, amendment noted in-file).

Citation rule inside ADRs: cite stable anchors (other ADRs by D-ID, doc sections, component/module names, requirement IDs) — never issue/PR numbers as forward pointers; issue numbers appear only as closed-issue provenance in status lines (AGENTS.md citation policy).

| ID | Status | Date | Title | File |
|----|--------|------|-------|------|
| D1 | accepted (reaffirmed 2026-09-24) | 2026-09-23 | Telemetry comes from hub-side code, not REPL polling | [adr/D1-telemetry-from-hub-code.md](adr/D1-telemetry-from-hub-code.md) |
| D2 | accepted | 2026-09-23 | Server-side BLE only; no Web Bluetooth anywhere | [adr/D2-server-side-ble-only.md](adr/D2-server-side-ble-only.md) |
| D3 | accepted | 2026-09-23 | Python-first; no word-blocks coding in v1 | [adr/D3-python-first.md](adr/D3-python-first.md) |
| D4 | accepted | 2026-09-23 | v1 telemetry wire: JSON lines over hub stdout | [adr/D4-stdout-json-lines.md](adr/D4-stdout-json-lines.md) |
| D5 | accepted | 2026-09-23 | Server is the source of truth for programs; hub storage optional | [adr/D5-server-source-of-truth.md](adr/D5-server-source-of-truth.md) |
| D-FL | accepted | 2026-09-24 | Firmware: Pybricks v4.0.1 stable; LEGO firmware backed up | [adr/D-FL-pybricks-v4-firmware.md](adr/D-FL-pybricks-v4-firmware.md) |
| D-GH | accepted | 2026-09-24 | Repository: `peara/brick-console`, private, docs split | [adr/D-GH-repo-setup.md](adr/D-GH-repo-setup.md) |
| D6 | accepted (amended 2026-09-25) | 2026-09-24 | All hub access goes through the `Transport` interface | [adr/D6-transport-seam.md](adr/D6-transport-seam.md) |
| D7 | accepted | 2026-09-25 | Telemetry wire schema: JSON lines over hub stdout; raw-log-primary fan-out | [adr/D7-telemetry-wire-schema.md](adr/D7-telemetry-wire-schema.md) |