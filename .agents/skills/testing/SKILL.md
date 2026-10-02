---
name: testing
description: Use when writing, running, or modifying tests in brick-console — pytest, conftest, fixtures, markers, the BRICK_CONSOLE_HUB_TESTS hardware gate, or ruff lint/format. Triggers: test, tests, testing, pytest, hub test, hub gate, marker, conftest, ruff, coverage, "run the suite".
---

# Testing in brick-console

Canonical rules live in `docs/testing.md` — read the relevant section before writing tests:

- Tests for `src/brick_console/` (server) → docs/testing.md § "Server tests"
- Tests for `agent/` (MicroPython hub agent) → docs/testing.md § "Hub-agent tests"
- Anything touching the physical hub → docs/testing.md § "The hub gate"

Safety lines — never violate (full rules in AGENTS.md):

1. Tests never move motors. Motor-driving verification is manual QA with desk-clear confirmation, not pytest.
2. `@pytest.mark.hub` means "talks to real hardware over BLE". These auto-skip unless `BRICK_CONSOLE_HUB_TESTS=1`.
3. `uv run pytest` must stay safe to run unattended with the hub off — hardware work stays behind the gate.
4. All Python goes through `uv`: `uv run pytest`, never bare `pytest` (hook-enforced).
5. **No BLE scan unless the owner says "go" in this conversation.** "The hub is on", staged tooling, or an earlier scan is not a go. The hub auto-sleeps when unconnected — a surprise scan window burns its awake time and has cost a real session (2026-10-03). When in doubt: report the blocked state and wait.

Commands:

```bash
uv run pytest                            # full suite; hub tests auto-skip
BRICK_CONSOLE_HUB_TESTS=1 uv run pytest  # hardware tests (hub on, desk clear, no motors)
uv run ruff check .                      # lint
uv run ruff format --check .             # formatting
uv run mypy src/                          # type check
```
