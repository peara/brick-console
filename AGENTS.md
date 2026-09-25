# AGENTS.md — working guide for coding agents in brick-console

Read fully before doing anything. This file is the playbook for any coding agent (Claude Code, Codex, Hermes, etc.) working in this repo.

## What this project is

brick-console is a self-hosted web console that manages a LEGO® MINDSTORMS® Robot Inventor 51515 hub running Pybricks v4.0.1 firmware. The server (an always-on Linux box with a BLE adapter) is the *only* BLE central; every other device is a browser client over LAN/Tailscale.

Product docs: [docs/brd.md](docs/brd.md) · [architecture.md](docs/architecture.md) · [decisions.md](docs/decisions.md) · [testing.md](docs/testing.md) · library reference: [docs/research/pybricksdev-api-notes.md](docs/research/pybricksdev-api-notes.md)

## Hard safety rules (physical hardware involved)

The hub is a physical robot. Software mistakes are cheap; hardware mistakes are not.

1. **Never flash firmware or restore backups without explicit user approval in the active conversation.** `pybricksdev flash` / `dfu restore` / `dfu backup` — always ask first, including "just re-flashing the same version".
2. **Never run a program that can move motors unless the user confirms the desk is clear.** Installing and running benign programs (LED, print, sensor read) is pre-approved. Anything driving a `Motor` is not.
3. **One BLE central at a time.** If the user is connected via Pybricks Code or anything else, don't attempt a competing connection. Wait or ask.
4. **The firmware backup is sacred.** `firmware/lego-original-inventor-hub.bin` (gitignored) and `~/hermes/m5stack/backups/lego-original-inventor-hub.bin` are the only copies of the original LEGO firmware. Never modify, move, or delete either. Before any `flash`/`restore`, verify md5 `d0c76999d50b209881e16fe374982267` and prefer the older copy at `~/hermes/m5stack/backups/`.
5. **No interactive scan loops when the hub is off.** If scanning finds no "Pybricks Hub", report it and stop; the user powers the hub on. (Exemption: the brick-console service's own bounded, backoff rescan loop is sanctioned — always-on auto-reconnect is the product's core requirement R1/F6. This rule is about *you*, the agent, at the terminal.)
6. **Stay off the hub's 5 permanent program slots** until BRD Q2 is resolved. Run-to-RAM only (`pybricksdev run ble` does this correctly).

Everything else — code, docs, tests, editor work, scanning, connecting, installing and running benign programs, telemetry, soak tests — proceeds without asking.

## Scope and environment

- **The repo is the workspace.** Don't create, modify, or delete anything outside it without asking (one exception: the firmware backup path in rule 4 is read-only reference).
- **All Python goes through uv, inside the repo.** Use `uv sync` / `uv run …` only. The bare `python3`/`pip` on PATH are not system interpreters — they live in a virtualenv outside this repo, so a plain `pip install` or `python script.py` silently uses and mutates a foreign environment. Never call them.
- **This rule is mechanically enforced** in every agent that supports hooks: a shared policy core (`hooks/uv-check.sh`) is wired into each tool — Claude Code (`.claude/settings.json`), Cursor (`.cursor/hooks.json`), Copilot (`.github/hooks/require-uv.json`), OpenCode (`opencode.json` permission rules). If your bare-Python command is denied, re-run it as `uv run …`. Agents without hook support: follow the same rule; it saves you a confusing debugging session.
- **Server box:** Linux (Ubuntu), user `peara`. BLE adapter hci0 (USB), BlueZ 5.72, `dfu-util` and udev rules installed. BLE access needs no root.
- **Hub:** advertises as "Pybricks Hub". Cached address `38:D3:4E:D4:E6:A1`, but the address can drift after re-flash/factory reset — re-scan by name rather than trusting the cached address.
- **Firmware state:** Pybricks v4.0.1 stable. Recovery is always possible via DFU mode (hold Bluetooth button + plug USB) — flash operations never touch the bootloader region, so a failed/corrupt flash leaves the hub recoverable. (The DFU re-entry drill on Pybricks firmware is still pending — verify once when convenient.)
- **Never commit:** `.venv/`, firmware binaries (`.zip`/`.bin`), `.env`. See `.gitignore`.

## Commands

```bash
uv sync                                                        # set up .venv
uv run pytest                                                  # tests (hub-marked tests auto-skip)
uv run ruff check .                                            # lint
uv run ruff format --check .                                   # formatting check
uv run pybricksdev run ble --name "Pybricks Hub" --wait <file>.py   # install + run a program (RAM)
uv run python -c "..."                                         # ad-hoc scripts
```

## Testing conventions

Full convention: [docs/testing.md](docs/testing.md) — strategies per target (server, hub agent, hardware). The invariants:

- `uv run pytest` is always safe to run: hardware tests are marked `@pytest.mark.hub` and auto-skip unless `BRICK_CONSOLE_HUB_TESTS=1`.
- Tests never move motors — motor-driving verification is manual QA with desk-clear confirmation, not pytest.

## Conventions

- Layout: `docs/` (documentation), `programs/` (user program library served by the console), `agent/` (hub-side `brick_telemetry` library), `src/brick_console/` (server package), `firmware/` (binaries gitignored, README documents state).
- Architecture-level changes (wire protocol, state model, storage) get an ADR first — a new file in [docs/adr/](docs/adr/) plus a row in the [decisions.md](docs/decisions.md) index; small stuff goes directly.
- Server code: async/await (pairs with bleak), type hints, ruff-format defaults.
- Hub code: MicroPython-compatible subset; f-strings are supported on Pybricks v4; keep modules small — compiled code and data live in limited user RAM.
- Commits: conventional commits (`feat:`, `fix:`, `docs:`, `chore:`).
- Delivery: implementation lands via **PR — never push `main` directly**. Branch `issue-<N>` → PR (`Closes #N`) → owner reviews → owner merges; the merge closes the issue. Docs-only mechanics also go via PR; the PR template (.github/PULL_REQUEST_TEMPLATE.md) is the checklist.

## Hub runtime facts (save yourself surprises)

- Pybricks runs **one user program at a time**; installing a new program requires the current one to be stopped first — the hub rejects program writes with `CommandError.BUSY` while a program runs, so the state machine must sequence stop-before-install. See the two-mode state model in architecture.md §4.
- There is **no ambient telemetry over BLE** — the dashboard requires the `brick_telemetry` agent program installed and running (decisions.md D1).
- `system.storage` on this hub: 512 bytes, cleared on firmware change. Nothing durable goes there.
- Run-to-RAM (`pybricksdev run ble`) does not overwrite the 5 permanent slots.
- BLE write chunk size is negotiated per connection (read from hub capabilities); pybricksdev handles this internally.
- CoreInk e-ink client (M4): full refresh 0.82 s, partial 0.24 s, keep ≥15 s between refreshes.

## When you're stuck

- Hub not advertising → it's off/asleep; ask the user to power it on.
- BLE connect keeps failing → another central probably holds the hub; ask.
- Flash/DFU trouble → pybricks/support discussion #688 is the troubleshooting reference. DFU entry (hold BT button + USB) works regardless of firmware state.
- Anything ambiguous about physical safety → ask. Always ask, don't assume.

## Task tracking

GitHub Issues in this repo, milestones M1–M5 (M1 exists; M2–M5 are created when reached). Check open issues before starting; close with evidence (command output, logs) when done.