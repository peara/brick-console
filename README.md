# brick-console

Self-hosted management console for the LEGO® MINDSTORMS® Robot Inventor 51515 hub (Pybricks firmware): server-side BLE, web dashboard, program install/run, live telemetry.

brick-console runs on an always-on Linux machine with a Bluetooth Low Energy adapter — that server holds the *only* BLE connection to the hub. Every other device — laptop, phone, tablet — is just a web browser on your LAN or Tailscale network: no Bluetooth hardware, nothing to install.

## Status

- **M0 bring-up ✅** (2026-09-24): hub flashed Pybricks v4.0.1, original LEGO firmware backed up, BLE hello-world run from the box.
- **M1 in progress:** read-only dashboard (BLE manager + hub agent + WebSocket telemetry + minimal web page).

See [docs/brd.md](docs/brd.md) for requirements and milestones.

## Docs

| Doc | Contents |
|---|---|
| [docs/brd.md](docs/brd.md) | Product: problem, vision, flows, requirements, milestones |
| [docs/architecture.md](docs/architecture.md) | System: components, protocols, state model, repo layout |
| [docs/decisions.md](docs/decisions.md) | ADR-style decision records (D1–D6, D-FL, D-GH) |
| [docs/testing.md](docs/testing.md) | Testing: strategies per target, the hub-test safety gate |
| [docs/research/investigation.md](docs/research/investigation.md) + [brd-v0.1-draft.md](docs/research/brd-v0.1-draft.md) | Frozen research archive (pre-repo investigation, original BRD draft) |
| [docs/research/pybricksdev-api-notes.md](docs/research/pybricksdev-api-notes.md) | Active library-API reference (Q1 spike; pins pybricksdev behavior) — not archived |

## Quick start (dev)

**Server (the machine with the Bluetooth adapter):** Linux with BlueZ, a BLE adapter, Python 3.12+, [uv](https://docs.astral.sh/uv/).
**Clients (any other device):** a web browser — nothing else.

```bash
uv sync                                   # create venv + install deps
uv run pybricksdev run ble --name "Pybricks Hub" --wait hello.py   # sanity check
uv run pytest                            # tests (hub tests auto-skip; BRICK_CONSOLE_HUB_TESTS=1 to run them)
uv run ruff check .                      # lint
```

## Hub recovery

The hub runs Pybricks v4.0.1. Original LEGO firmware backup lives outside the repo at `~/hermes/m5stack/backups/lego-original-inventor-hub.bin` (md5 `d0c76999…`). To restore: hub in DFU mode (hold Bluetooth button + plug USB), then `uv run pybricksdev dfu restore <backup-file>`.

## Repository layout

```
docs/               # project documentation
programs/           # user program library (served by the console)
agent/              # hub-side brick_telemetry library + agent wrapper (M1)
src/brick_console/  # server package (M1)
tests/              # pytest suite (hub-marked hardware tests gated)
hooks/              # uv-enforcement policy core wired into coding agents
firmware/           # firmware archives (gitignored; see firmware/README.md)
hello.py            # M0 bring-up artifact (BLE sanity program)
```

## License

Private project — all rights reserved (license may change later).