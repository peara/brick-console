# Testing convention

How code is tested in brick-console. This is the canonical doc: coding agents follow it when writing or running tests. AGENTS.md carries only the safety invariants; the full rules live here.

Established 2026-09-24 with the tooling baseline (issue #1). This doc grows with M1 — each rule exists because something already needs it; nothing here is speculative.

## Principles

1. **The default suite is safe to run any time.** `uv run pytest` must be safe unattended, with the hub off, with no BLE. Everything hardware-dependent sits behind the `hub` gate.
2. **Three targets, three strategies:**

| Target | Where | Strategy | In default suite |
|---|---|---|---|
| Server | `src/brick_console/` | pytest unit tests; asyncio auto mode | yes |
| Hub agent | `agent/` (MicroPython) | host unit tests via import-safe modules + stubs | yes |
| Real hardware | hub over BLE | `@pytest.mark.hub` gated tests + scripted on-hub verification | gated |

3. **Tests prove behavior, not implementation.** Test the observable outcome (parsed frame, state transition, emitted line), not the private path.

## Commands

```bash
uv run pytest                                   # full suite; hub tests auto-skip
BRICK_CONSOLE_HUB_TESTS=1 uv run pytest         # opt in to hardware tests
uv run ruff check .                             # lint
uv run ruff format --check .                    # formatting
```

All Python goes through `uv` (AGENTS.md) — bare `pytest` is denied by the agent hooks.

## The hub gate (hardware tests)

- **Marker:** `@pytest.mark.hub` means "this test talks to the physical hub over BLE from the server box."
- **Gate:** `tests/conftest.py` skips every `hub`-marked test unless `BRICK_CONSOLE_HUB_TESTS=1` (exact string). This lets a coding agent run the full suite safely while the hub is off.
- **What qualifies:** only things real hardware can prove — advertising, connection, the stdio pipe, run-to-RAM program lifecycle. If a fake can prove it, it's not a hub test.
- **Safety inside hub tests:** never move motors. Installing and running benign programs (LED, print, sensor read) is pre-approved; anything driving a `Motor` is **manual QA with desk-clear confirmation** — never pytest.
- `tests/test_hub_smoke.py` is a placeholder proving the gate machinery; the M1 finale replaces it with the real hardware smoke test.

## Server tests (`src/brick_console/`)

- Plain pytest + pytest-asyncio **auto mode** — `async def test_*` needs no marker.
- Unit-test the pure logic first: parsers, protocol frames, state-model transitions. No network, no BLE.
- The suite never touches real BLE. All BLE-adjacent server code goes through the `Transport` seam (decision D6) — fake the `Transport` in tests, not bleak internals scattered through call sites. The fake drives the state machine exactly the way the real adapter will.
- WS gateway, state model, web handlers: ordinary unit tests like everything else.

## Hub-agent tests (`agent/` — MicroPython)

There is no pytest on the hub. Two stages:

1. **Host unit tests** (default suite, no marker): the agent's pure logic must run on the host. Structure `agent/` code so logic modules import cleanly — no hardware I/O at import time — and the thin pybricks-dependent shell stays separate. Where imports can't be avoided, a conftest stub (fake `pybricks.*` modules in `sys.modules`) bridges them.
2. **On-hub verification:** scripted asserts + observed stdout via `uv run pybricksdev run ble --name "Pybricks Hub" --wait <file>.py` — read the output, compare against expected. Manual/scripted step, not part of pytest.

## Layout & naming

- Tests live in `tests/`, mirroring the package layout as `src/brick_console/` grows (flat is fine while the package is one module).
- Files named `test_<module>.py`; shared fixtures in the single `tests/conftest.py` until growth forces more.
- Only the `hub` marker is registered. A new marker needs a `pyproject.toml` entry plus a line here.

## Deliberately not (yet)

- **No CI workflow** — deferred by choice (single-user private repo). Revisit when the suite carries real weight.
- **No fake-hub/sim tier yet** — now unblocked: the wire-schema ADR (D7) defines what a fake hub must simulate — CRLF-terminated canonical lines split at arbitrary chunk boundaries (including mid-CRLF), all four kinds at cadence, occasional malformed lines, occasional unknown kinds. The WS gateway's mock source (`?mock=1`, architecture §2.3) is the first instance of that tier. The `Transport` seam (D6) is what BLE-adjacent tests fake meanwhile.
- **No motor tests, ever** — manual QA only (AGENTS.md safety rule 2).