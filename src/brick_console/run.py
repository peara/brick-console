"""The ``brick-console`` run command (issue #10): serve the app via uvicorn.

This exact command is what the systemd unit executes (its deliverable pins
``ExecStart``). It runs uvicorn programmatically — ``uvicorn.run`` — so the
process is the server: one process, one event loop, the manager task and
the web tier sharing it (architecture §8: async-native pairs with bleak;
no worker processes — the app owns the single BLE central role, F6, and
multiple workers would fight over it).

Manager wiring today: a *stub*, per the issue ("the manager handle can be
a stub now"). The real manager needs the hub-side agent program
(``agent/agent_main.py``, not yet landed) to install; until then the run
command serves with ``StubManager`` — hub state pinned to ``offline``, no
BLE touched. When the agent lands, this stub is replaced by constructing
the real ``BLEManager`` over the bleak adapter (:mod:`brick_console.adapter`)
and the ``TelemetryStore`` (:mod:`brick_console.store`) — the seams
already exist and are injected through ``create_app`` (no globals).

Configuration (environment):

- ``BRICK_CONSOLE_HOST`` — bind address, default ``0.0.0.0`` (bind-policy
  decision, README ops note: NAT posture — the box is not port-forwarded,
  so 0.0.0.0 means "every interface = LAN + Tailscale", and the firewall
  keeps it LAN-only; ``BRICK_CONSOLE_HOST=127.0.0.1`` or a specific
  interface address is the stricter escape hatch).
- ``BRICK_CONSOLE_PORT`` — bind port, default ``8300``.

Logging: uvicorn's default access/error logging is kept — structured enough
for ``journalctl`` (timestamps, level, client address on access lines); no
extra JSON formatter (M1: keep it simple, the unit's deliverable documents
what lands in the journal). The manager task logs through the same config.

Exit code 0 on clean shutdown (SIGTERM from systemd → uvicorn handles →
lifespan cancels the manager task); anything else is a crash the unit's
``Restart=on-failure`` will retry — R1 applies to the whole service.
"""

from __future__ import annotations

import asyncio
import os

import uvicorn

from brick_console.app import create_app

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "StubManager",
    "bind_config",
    "main",
]

DEFAULT_HOST = "0.0.0.0"
"""Bind-policy default (README ops note): NAT posture — every interface on
a box that is not port-forwarded; exposure is LAN/Tailscale only (§6)."""

DEFAULT_PORT = 8300
"""Default port pinned by the issue; overridable via ``BRICK_CONSOLE_PORT``."""


class StubManager:
    """Placeholder manager (issue #10: "the manager handle can be a stub
    now"): reports the hub as offline forever, ``run()`` parks until
    cancelled (lifespan shutdown). The real ``BLEManager`` replaces this
    once the agent program (``agent/``) lands — same injected seam, zero
    app changes (the run command constructs it here and passes it to
    :func:`~brick_console.app.create_app`).
    """

    def __init__(self) -> None:
        self.started = 0

    @property
    def state(self) -> str:
        return "offline"

    @property
    def state_reason(self) -> str:
        return "manager not wired yet (stub)"

    async def run(self) -> None:
        # Per-invocation park latch: an asyncio.Event created outside the
        # loop binds to the first loop that awaits it, so a second lifespan
        # on the same app (uvicorn --reload, or a test re-entering a
        # client) would hit "Event is bound to a different event loop" and
        # silently kill the manager task. Creating it inside run() binds it
        # to the current loop every time.
        self.started += 1
        await asyncio.Event().wait()


def bind_config(env: dict[str, str] | None = None) -> tuple[str, int]:
    """Resolve ``(host, port)`` from the environment (or the given mapping,
    for tests). Port parsing is strict: a non-integer or out-of-range
    ``BRICK_CONSOLE_PORT`` crashes at startup with a clear message — a
    server silently binding to the default port after the operator
    explicitly asked for another is worse than a fail-fast (fail loudly,
    R1's spirit: the service must come up correctly, or not at all).
    """
    env = os.environ if env is None else env
    host = env.get("BRICK_CONSOLE_HOST", DEFAULT_HOST)
    port_raw = env.get("BRICK_CONSOLE_PORT", str(DEFAULT_PORT))
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit(
            f"brick-console: invalid BRICK_CONSOLE_PORT={port_raw!r} — must be an integer"
        ) from exc
    if not 1 <= port <= 65535:
        raise SystemExit(
            f"brick-console: invalid BRICK_CONSOLE_PORT={port_raw!r} — "
            f"must be within 1-65535, got {port}"
        )
    return host, port


def main() -> None:
    """Console-script entry point (``[project.scripts]`` → ``brick-console``)."""
    host, port = bind_config()
    app = create_app(StubManager())
    # uvicorn's default logging config (log_config untouched) is the
    # journalctl story: formatted INFO lines — startup, shutdown, access —
    # on stderr, which systemd captures. Passing log_config=None would
    # *disable* logging setup and silently drop every line.
    uvicorn.run(app, host=host, port=port)
