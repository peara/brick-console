"""The ``brick-console`` run command (issue #10): serve the app via uvicorn.

This exact command is what the systemd unit executes (its deliverable pins
``ExecStart``). It runs uvicorn programmatically — ``uvicorn.run`` — so the
process is the server: one process, one event loop, the manager task and
the web tier sharing it (architecture §8: async-native pairs with bleak;
no worker processes — the app owns the single BLE central role, F6, and
multiple workers would fight over it).

Manager wiring: the real :class:`~brick_console.ble_manager.BLEManager` over
the bleak/pybricksdev adapter (:mod:`brick_console.adapter`) — same injected
seams as the stub, zero app changes (issue #15). The imports live inside
``main()`` on purpose: importing ``brick_console.run`` must stay lightweight
(no bleak, no pybricksdev) so the offline fallback path and the module
isolation tests hold. :class:`StubManager` stays in this module as the
documented fallback for BLE-less environments — it is simply no longer what
``main()`` wires.

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
what lands in the journal). uvicorn's dictConfig equips only its own
``uvicorn.*`` loggers, so ``main()`` also installs a root handler
(``logging.basicConfig``) for the app's own loggers — the manager's state
transitions and malformed-line counts (``brick_console.*``, INFO) would
otherwise be dropped: they propagate to an unconfigured root whose
last-resort handler emits WARNING+ only. Invisible with the stub (it never
logged); observable the day the real manager was wired.

Exit behavior: SIGINT exits 0. SIGTERM triggers uvicorn's graceful shutdown
(lifespan cancels the manager task, port released) and then exits 143 —
uvicorn re-raises the captured signal after the graceful sequence; that is
upstream ``capture_signals`` behavior, not a crash. For systemd this is
correct-and-safe: dying by its stop signal counts as a clean stop, so the
unit's ``Restart=always`` neither loops nor marks the service failed. A
configuration typo (invalid port) exits non-zero before any socket is
opened — systemd's restart backoff handles that without port flapping.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

import uvicorn

from brick_console.app import create_app
from brick_console.store import TelemetryStore

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


def _package_version() -> str:
    """The installed distribution's version — /healthz must report what is
    actually running, not a literal that drifts on the next version bump."""
    try:
        return pkg_version("brick-console")
    except PackageNotFoundError:
        # Not installed (e.g. PYTHONPATH-style use without uv sync): fall
        # back to the source tree's declared version.
        return "0.1.0"


class StubManager:
    """Offline fallback manager (kept from issue #10): reports the hub as
    offline forever, ``run()`` parks until cancelled (lifespan shutdown).
    The run command wires the real
    :class:`~brick_console.ble_manager.BLEManager` since the agent program
    (``agent/``) landed; this class remains for BLE-less environments and
    the module-isolation tests — same injected seam, zero app changes.

    Subscription no-ops: the WS gateway subscribes to the state,
    telemetry, and raw-line fan-outs; a manager that lacks them would
    crash the gateway. The stub therefore accepts every subscription
    and simply never fires it — an offline stub has no state changes
    (state is pinned to ``offline``) and produces no telemetry or stdout.
    The gateway still serves the store (empty until the real manager
    lands) and the join sequence, so the dashboard works end-to-end the
    day this stub is swapped out.
    """

    def __init__(self) -> None:
        self.started = 0

    @property
    def state(self) -> str:
        return "offline"

    @property
    def state_reason(self) -> str:
        return "manager not wired yet (stub)"

    def subscribe_state(self, listener: object) -> Callable[[], None]:
        """Accept and never fire — see the class docstring."""
        return _noop_unsubscribe

    def subscribe_telemetry(self, listener: object) -> Callable[[], None]:
        """Accept and never fire — see the class docstring."""
        return _noop_unsubscribe

    def subscribe_raw(self, listener: object) -> Callable[[], None]:
        """Accept and never fire — see the class docstring."""
        return _noop_unsubscribe

    async def run(self) -> None:
        # Per-invocation park latch: an asyncio.Event created outside the
        # loop binds to the first loop that awaits it, so a second lifespan
        # on the same app (uvicorn --reload, or a test re-entering a
        # client) would hit "Event is bound to a different event loop" and
        # silently kill the manager task. Creating it inside run() binds it
        # to the current loop every time.
        self.started += 1
        await asyncio.Event().wait()


def _noop_unsubscribe() -> None:
    """Idempotent no-op the stub subscriptions return."""


def bind_config(env: Mapping[str, str] | None = None) -> tuple[str, int]:
    """Resolve ``(host, port)`` from the environment (or the given mapping,
    for tests). Port parsing is strict: anything other than a plain
    integer string in 1-65535 — no whitespace, no sign, no decimal —
    crashes at startup with a clear message. A server silently binding to
    the default port after the operator explicitly asked for another is
    worse than a fail-fast (fail loudly, R1's spirit: the service must
    come up correctly, or not at all).
    """
    env = os.environ if env is None else env
    host = env.get("BRICK_CONSOLE_HOST", DEFAULT_HOST)
    port_raw = env.get("BRICK_CONSOLE_PORT", str(DEFAULT_PORT))
    # isascii() matters: str.isdigit() alone accepts Unicode digit
    # characters (superscript "²") that int() then rejects — an uncaught
    # ValueError instead of the promised clean SystemExit.
    if not (
        port_raw.isascii()
        and port_raw.isdigit()
        and 1 <= (port := int(port_raw)) <= 65535
    ):
        raise SystemExit(
            f"brick-console: invalid BRICK_CONSOLE_PORT={port_raw!r} — "
            f"must be a plain integer within 1-65535"
        )
    return host, port


def main() -> None:
    """Console-script entry point (``[project.scripts]`` → ``brick-console``)."""
    host, port = bind_config()
    # Real manager wiring (issue #15): BLEManager over the bleak/pybricksdev
    # adapter, same injected seams as the stub — zero app changes. Imports
    # stay local so importing this module pulls in no BLE stack: the
    # offline fallback (StubManager) and the module-isolation tests
    # depend on brick_console.run staying import-light.
    from brick_console.adapter import PybricksDevTransport
    from brick_console.ble_manager import BLEManager

    # uvicorn's dictConfig equips only its uvicorn.* loggers; the app's own
    # INFO lines (manager state transitions, malformed-line counts) would
    # otherwise drop at the unconfigured root (last-resort emits WARNING+).
    # level=INFO is load-bearing: root defaults to WARNING. The handler
    # survives uvicorn's config (dictConfig disables no existing loggers).
    logging.basicConfig(level=logging.INFO)

    store = TelemetryStore()
    app = create_app(
        BLEManager(PybricksDevTransport(), store),
        store=store,
        version=_package_version(),
    )
    # uvicorn's default logging config (log_config untouched) is the
    # journalctl story: formatted INFO lines — startup, shutdown, access —
    # on stderr, which systemd captures. Passing log_config=None would
    # *disable* logging setup and silently drop every line.
    uvicorn.run(app, host=host, port=port)
