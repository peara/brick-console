"""FastAPI app factory: the web tier's shell (issue #10, architecture §8).

M1 scope is deliberately thin: serve the dashboard assets (no build step —
the files land with the dashboard deliverable), expose ``GET /healthz`` for
the smoke test and systemd tooling to poll, and pin the run command the
systemd unit executes. No auth (M2+, optional, off by default), no library
CRUD (M3), no editor scaffolding — resist the scope creep.

The factory pattern exists for testability and wiring (architecture §2.1):
``create_app(...)`` takes the BLE manager as an injected seam — no globals —
so the WebSocket gateway, the dashboard files, and later editor/library
views wire in through constructor arguments, not module imports. FastAPI's
*lifespan* starts and stops the manager task: the app owns the process's
single manager (which holds the single BLE central role, F6) — one instance
per server process, started once per lifespan, so there is never a second
competing central.

The manager seam is deliberately narrow (a structural protocol, not the
concrete class — same discipline as the ``Transport`` seam, D6): the app
needs only the always-on ``run()`` coroutine plus read-only state for
``/healthz``. The concrete :class:`~brick_console.ble_manager.BLEManager`
satisfies it structurally (``HubState`` is a ``StrEnum``, i.e. a ``str``);
tests drive fakes, and the run command (:mod:`brick_console.run`) passes a
stub until the hub-side agent program (``agent/``) exists and the real
manager can be constructed.

Serving: static files ship inside the package (``src/brick_console/static/``
per the issue) so an installed copy carries the dashboard; no build step,
no Node toolchain (architecture §8: "no build tooling for M1").
``StaticFiles`` is mounted at ``/`` as the *fallback* route — API routes
registered before the mount win, so ``/healthz`` (and the WebSocket route,
added with the WS-gateway deliverable) take precedence over files.
``html=True`` gives the single-page dashboard its ``/`` → ``index.html``
entry; the directory must exist (``check_dir=True`` fails the factory
loudly on a bad override — the packaged default ships with the
placeholder, and an empty-but-existing override dir still boots and 404s).

Uptime: monotonic, not wall-clock (``time.monotonic()`` delta from lifespan
start) so NTP jumps or suspend/resume can never make the reported uptime
negative or jumpy — the same injectable-clock discipline as the BLE
manager (testing.md).

Bind policy (decided in this PR, recorded in the README ops note): default
host ``0.0.0.0`` behind the box's NAT, LAN/Tailscale exposure only, never
port-forwarded; ``BRICK_CONSOLE_HOST`` is the stricter-posture escape
hatch. Rationale in the README.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from brick_console.store import TelemetryStore

__all__ = [
    "ManagerHandle",
    "create_app",
    "healthz_payload",
]

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
"""Dashboard assets shipped inside the package — served from there so an
installed console carries the UI with no build step (architecture §8). The
placeholder ``index.html`` lands with this module; the real dashboard files
replace it in a later PR."""


class ManagerHandle(Protocol):
    """The narrow manager seam the app consumes (structural, like D6).

    Satisfied structurally by
    :class:`~brick_console.ble_manager.BLEManager`; tests and the run
    command drive fakes/stubs through the same interface, so the WS
    gateway and the systemd unit wire in without globals or isinstance
    checks. ``state`` and ``state_reason`` back ``/healthz``'s hub block —
    exactly the surface the state feed (the manager's public API) exposes.
    """

    @property
    def state(self) -> str:
        """Server-tracked hub state — one of the architecture §4 values
        (offline / advertising / agent / program), as a plain string so
        fakes need no enum import."""

    @property
    def state_reason(self) -> str:
        """Why the current state was entered (last transition's reason)."""

    async def run(self) -> None:
        """The always-on loop (R1): never returns normally — cancelled at
        lifespan shutdown; a fake may return or park forever."""


def healthz_payload(
    hub_state: str,
    hub_state_reason: str,
    *,
    started_at: float,
    now: float,
    version: str = "0.1.0",
) -> dict[str, object]:
    """Build the ``/healthz`` JSON body — pure, so tests pin the shape::

        {
          "status": "ok",
          "version": "0.1.0",
          "uptime_s": 12.5,
          "server": {"state": "running"},
          "hub": {"state": "offline", "reason": "…"}
        }

    ``uptime_s`` is a monotonic delta (``now - started_at``), clamped at 0
    and rounded to milliseconds. The smoke test and systemd tooling only
    rely on the 200; the dashboard and future probes may read the fields,
    so the shape is pinned by tests.
    """
    return {
        "status": "ok",
        "version": version,
        "uptime_s": round(max(0.0, now - started_at), 3),
        "server": {"state": "running"},
        "hub": {"state": hub_state, "reason": hub_state_reason},
    }


def create_app(
    manager: ManagerHandle,
    *,
    store: TelemetryStore | None = None,
    version: str = "0.1.0",
    static_dir: Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the FastAPI app with the manager seam injected (no globals).

    Lifespan (see :func:`_lifespan`) starts the manager's always-on
    ``run()`` as a background task at startup and cancels it at shutdown,
    releasing the BLE central role cleanly (F6). A manager task that dies
    is logged, never fatal — the web tier keeps serving with the hub down;
    retrying is the manager's own loop's job (R1), not the app's crash.

    ``store`` is the injected telemetry-store handle (the ring buffer the
    WS gateway will replay from, architecture §2.1): stored on
    ``app.state`` for the WS-gateway deliverable to consume — no globals.
    ``None`` builds a bare app (the run command passes a real one).

    ``static_dir`` overrides the packaged dashboard directory (default
    ``src/brick_console/static/``) — tests point it at fixtures instead of
    mutating the package. The override must exist: ``check_dir=True``
    fails the factory loudly on a bad path rather than serving 500s at
    request time (the packaged default always exists; an empty override
    dir still boots and 404s). ``monotonic`` feeds uptime; tests pass
    deterministic fakes (the same injectable-clock discipline as the BLE
    manager, testing.md).
    """
    app = FastAPI(title="brick-console", version=version, lifespan=_lifespan)
    app.state.manager = manager
    app.state.store = store
    app.state.version = version
    app.state.monotonic = monotonic
    # Fallback if a request runs outside the lifespan context (uptime then
    # counts from factory time); the lifespan re-stamps on startup.
    app.state.started_at = monotonic()
    app.state.manager_task: asyncio.Task[None] | None = None

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        # Read the seams through app.state (not the factory closure) so
        # every consumer — endpoint, WS gateway, tests — sees one source
        # of truth if a handle is ever re-wired on the state object.
        return healthz_payload(
            app.state.manager.state,
            app.state.manager.state_reason,
            started_at=app.state.started_at,
            now=app.state.monotonic(),
            version=app.state.version,
        )

    # Fallback mount last: routes registered above win over files; the
    # dashboard lands as plain files under static_dir. check_dir=True: a
    # bad static_dir fails at factory time (loud, early) instead of
    # answering 500 on every static request later; the packaged default
    # ships with index.html so it always passes the check.
    app.mount(
        "/",
        StaticFiles(directory=static_dir or _STATIC_DIR, html=True, check_dir=True),
        name="static",
    )
    return app


async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan manager: run the manager task alongside the web tier.

    Startup: stamp the monotonic start mark (uptime = time serving) and
    launch the manager's always-on loop as a named background task (the
    name surfaces in "Task was destroyed but it is pending!" style journal
    noise, so make it identifiable). Shutdown: cancel and await the task
    so the BLE central role is always released (F6). Cancelling an
    already-finished task is a no-op, so shutdown stays safe in every case.
    """
    manager: ManagerHandle = app.state.manager
    app.state.started_at = app.state.monotonic()
    task = asyncio.create_task(_run_manager(manager), name="ble-manager")
    app.state.manager_task = task
    logger.info("brick-console %s starting", app.state.version)
    try:
        yield
    finally:
        logger.info("brick-console stopping; releasing manager task")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        app.state.manager_task = None


async def _run_manager(manager: ManagerHandle) -> None:
    """Drive the manager's always-on loop; a dead manager never kills the
    web tier. The manager's own never-give-up boundary (R1) catches
    ordinary failures inside ``run()``, so reaching a return or an
    exception here is a hard-stop condition: log loudly and let the task
    end — shutdown joins it either way, and the web tier stays up."""
    try:
        await manager.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("BLE manager task ended unexpectedly; web tier continues")
    else:
        logger.warning("BLE manager loop returned; web tier continues without hub")
