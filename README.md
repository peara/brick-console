# brick-console

Self-hosted management console for the LEGO® MINDSTORMS® Robot Inventor 51515 hub (Pybricks firmware): server-side BLE, web dashboard, program install/run, live telemetry.

The Linux gateway box holds the one BLE connection to the hub; any browser on LAN/Tailscale gets a live dashboard, program editor, and console — no Bluetooth hardware needed on the client.

## Status

- **M0 bring-up ✅** (2026-09-24): hub flashed Pybricks v4.0.1, original LEGO firmware backed up, BLE hello-world run from the box.
- **M1 in progress:** read-only dashboard (BLE manager + hub agent + WebSocket telemetry + minimal web page).

See [docs/brd.md](docs/brd.md) for requirements and milestones.

## Docs

| Doc | Contents |
|---|---|
| [docs/brd.md](docs/brd.md) | Product: problem, vision, flows, requirements, milestones |
| [docs/architecture.md](docs/architecture.md) | System: components, protocols, state model, repo layout |
| [docs/decisions.md](docs/decisions.md) | ADR-style decision records (D1–D5, D-FL, D-GH) |
| [docs/research/](docs/research/) | Investigation + original BRD draft (frozen archive) |

## Quick start (dev)

Requires: Linux with BlueZ, a BLE adapter, Python 3.12+, [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # create venv + install deps
uv run pybricksdev run ble --name "Pybricks Hub" --wait hello.py   # sanity check
```

## Hub recovery

The hub runs Pybricks v4.0.1. Original LEGO firmware backup lives outside the repo at `~/hermes/m5stack/backups/lego-original-inventor-hub.bin` (md5 `d0c76999…`). To restore: hub in DFU mode (hold Bluetooth button + plug USB), then `uv run pybricksdev dfu restore <backup-file>`.

## Repository layout

```
docs/               # project documentation
programs/           # user program library (served by the console)
agent/              # hub-side brick_telemetry library (M1)
src/brick_console/  # server package (M1)
firmware/           # firmware archives (gitignored; see firmware/README.md)
```

## License

Private project — all rights reserved (license may change later).