"""Tests for :mod:`brick_console.app` (issue #10) — the FastAPI shell.

Each test maps to one Done-when checkbox from the issue. TestClient only
(no live socket): the lifespan runs inside the client's event loop, so the
manager-task wiring is exercised exactly as the real server runs it — with
a fake manager whose start/stop is observable.

No BLE, no hub: the manager seam is faked (D6 discipline — fake the seam,
not FastAPI internals). All tests are plain sync functions; TestClient
drives its own loop (testing.md: the WS route is a later deliverable, so
no async tests are needed here).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from brick_console.app import create_app, healthz_payload
from brick_console.ble_manager import BLEManager
from brick_console.store import TelemetryStore
from brick_console.transport import Transport


@dataclass
class FakeManager:
    """Records lifespan-driven start/stop; parks in ``run()`` until cancelled.

    Satisfies the ``ManagerHandle`` protocol structurally — the same way
    the real ``BLEManager`` does (a structural seam, like the Transport
    seam, D6).
    """

    state: str = "offline"
    state_reason: str = "fake manager: hub not scanned yet"
    started_count: int = 0
    cancelled: bool = False
    task: asyncio.Task[None] | None = None
    loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        self.started_count += 1
        self.task = asyncio.current_task()
        self.loop = asyncio.get_running_loop()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    def set_state(self, state: str, reason: str) -> None:
        self.state = state
        self.state_reason = reason


# ---------------------------------------------------------------------------
# create_app factory + injected seams; lifespan wires the manager task
# ---------------------------------------------------------------------------


def test_create_app_returns_fastapi_with_injected_manager() -> None:
    manager = FakeManager()
    app = create_app(manager)

    assert app.title == "brick-console"
    # The seam is injected, not global: app.state carries the instance.
    assert app.state.manager is manager


def test_create_app_injects_store_handle() -> None:
    store = TelemetryStore()
    app = create_app(FakeManager(), store=store)

    assert app.state.store is store
    # Default: no store — a bare app is constructible for thin tests.
    assert create_app(FakeManager()).state.store is None


def test_lifespan_starts_and_cancels_manager_task() -> None:
    manager = FakeManager()
    app = create_app(manager)

    with TestClient(app):
        assert manager.started_count == 1
        assert app.state.manager_task is not None
        assert not app.state.manager_task.done()
        assert manager.task is app.state.manager_task
        assert manager.loop is not None

    # Lifespan exit cancelled and joined the manager task (F6 released).
    assert manager.cancelled
    assert app.state.manager_task is None


def test_healthz_200_while_manager_task_live() -> None:
    manager = FakeManager()
    with TestClient(create_app(manager)) as client:
        assert client.get("/healthz").status_code == 200
    assert manager.started_count == 1


def test_dead_manager_task_web_tier_survives() -> None:
    # A manager whose run() returns immediately (hard-stop) must not crash
    # the app: /healthz still 200 after the task ends; a follow-up request
    # is still served.

    class DyingManager(FakeManager):
        async def run(self) -> None:
            self.started_count += 1

    manager = DyingManager()
    with TestClient(create_app(manager)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200
    assert manager.started_count == 1


# ---------------------------------------------------------------------------
# Real-seam composition: the app wires the actual manager + store
# ---------------------------------------------------------------------------


def test_real_manager_and_store_compose_into_the_app() -> None:
    # The issue wires the app to the existing seams — do not reimplement
    # them. This pins that the real BLEManager satisfies the app's
    # ManagerHandle protocol structurally (state/state_reason/run) and the
    # real TelemetryStore is accepted as the injected store handle, so a
    # future signature drift on either seam fails here, not in production.
    # FakeTransport is re-declared locally (tests/ is not a package); its
    # discover() raises TimeoutError = "hub off", so the manager's loop
    # parks in OFFLINE exactly as it would with the real hub powered down.

    class HubOffTransport(Transport):
        async def discover(self, name, *, timeout=10.0):
            raise TimeoutError()

        async def connect(self, hub, *, on_disconnect): ...

        async def install_and_start(self, program, *, wait=False): ...

        async def stop(self): ...

        async def write_stdin(self, data): ...

        async def subscribe_stdout(self, listener): ...

        async def subscribe_status(self, listener): ...

        async def disconnect(self): ...

    store = TelemetryStore(event_capacity=8)
    manager = BLEManager(HubOffTransport(), store)
    app = create_app(manager, store=store)

    assert app.state.manager is manager
    assert app.state.store is store
    with TestClient(app) as client:
        body = client.get("/healthz").json()
    # Manager parks in OFFLINE with the real state machine running; the
    # exact reason depends on loop timing ("service starting…" vs. the
    # first scan-miss note), but it is always the OFFLINE story.
    assert body["hub"]["state"] == "offline"
    assert body["hub"]["reason"]


# ---------------------------------------------------------------------------
# GET /healthz — server + hub state (fake-fed), 200
# ---------------------------------------------------------------------------


def test_healthz_returns_200_and_server_plus_hub_state() -> None:
    manager = FakeManager(state="agent", state_reason="agent installed and started")
    with TestClient(create_app(manager)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["status"] == "ok"
    assert body["hub"] == {"state": "agent", "reason": "agent installed and started"}
    assert body["server"] == {"state": "running"}


def test_healthz_reflects_manager_state_changes() -> None:
    # The hub block reads the manager seam at request time, not a snapshot
    # taken at startup: fake-feed two states through the same client.
    manager = FakeManager(state="offline", state_reason="hub not found (scan timeout)")
    with TestClient(create_app(manager)) as client:
        first = client.get("/healthz").json()["hub"]
        manager.set_state("advertising", "hub discovered: Pybricks Hub")
        second = client.get("/healthz").json()["hub"]

    assert first["state"] == "offline"
    assert second == {"state": "advertising", "reason": "hub discovered: Pybricks Hub"}


def test_healthz_uptime_monotonic_and_growing() -> None:
    # Deterministic monotonic fake: the lifespan stamps started_at; each
    # request computes now - started_at. Set `now` BEFORE entering the
    # client (startup stamps started_at with the current fake value).
    now = 100.0

    def fake_monotonic() -> float:
        return now

    manager = FakeManager()
    app = create_app(manager, monotonic=fake_monotonic)

    with TestClient(app) as client:
        first = client.get("/healthz").json()["uptime_s"]
        now = 107.25
        second = client.get("/healthz").json()["uptime_s"]

    assert first == 0.0
    assert second == 7.25


def test_healthz_payload_shape_pinned() -> None:
    payload = healthz_payload("offline", "x", started_at=10.0, now=12.5)
    assert payload == {
        "status": "ok",
        "version": "0.1.0",
        "uptime_s": 2.5,
        "server": {"state": "running"},
        "hub": {"state": "offline", "reason": "x"},
    }
    # Negative deltas clamp to 0 — uptime is never negative.
    assert healthz_payload("offline", "x", started_at=10.0, now=9.0)["uptime_s"] == 0.0


# ---------------------------------------------------------------------------
# Static serving — correct content types, no build step
# ---------------------------------------------------------------------------


def test_static_index_served_with_correct_content_type(tmp_path) -> None:
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body>fixture dashboard</body></html>"
    )
    with TestClient(create_app(FakeManager(), static_dir=tmp_path)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "fixture dashboard" in response.text


def test_static_file_content_types(tmp_path) -> None:
    # One file per type the dashboard will ship; each must get its correct
    # content type (the issue's "Content types correct" clause).
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "app.js").write_text("console.log('brick')")
    (tmp_path / "style.css").write_text("body{}")
    (tmp_path / "favicon.ico").write_bytes(b"\x00\x00\x00\x00")

    with TestClient(create_app(FakeManager(), static_dir=tmp_path)) as client:
        assert (
            client.get("/app.js").headers["content-type"].startswith("text/javascript")
        )
        assert client.get("/style.css").headers["content-type"].startswith("text/css")
        assert client.get("/favicon.ico").headers["content-type"].startswith("image/")
        assert client.get("/").headers["content-type"].startswith("text/html")


def test_packaged_static_dir_serves_placeholder() -> None:
    # The shipped placeholder (src/brick_console/static/index.html) serves
    # at / — proves the package actually ships servable assets, not just
    # that a fixture directory could work (packaging check).
    with TestClient(create_app(FakeManager())) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "brick-console" in response.text


def test_healthz_beats_static_mount(tmp_path) -> None:
    # API routes registered before the / mount win: with an index.html
    # present, GET /healthz still hits the endpoint.
    (tmp_path / "index.html").write_text("<html></html>")
    with TestClient(create_app(FakeManager(), static_dir=tmp_path)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").json()["status"] == "ok"


def test_missing_static_file_is_404_not_500(tmp_path) -> None:
    with TestClient(create_app(FakeManager(), static_dir=tmp_path)) as client:
        assert client.get("/nope.js").status_code == 404


def test_nonexistent_static_dir_fails_at_factory_time(tmp_path) -> None:
    # check_dir=True: a bad static_dir must fail loudly at create_app
    # (RuntimeError), never boot a server whose every static request
    # would 500. The packaged default always exists, so only a bad
    # override hits this.
    with pytest.raises(RuntimeError, match="does not exist"):
        create_app(FakeManager(), static_dir=tmp_path / "nope")


# ---------------------------------------------------------------------------
# Run command — bind config honored by the entry point
# ---------------------------------------------------------------------------


def test_bind_config_defaults() -> None:
    from brick_console.run import DEFAULT_HOST, DEFAULT_PORT, bind_config

    assert DEFAULT_HOST == "0.0.0.0"
    assert DEFAULT_PORT == 8300
    assert bind_config({}) == ("0.0.0.0", 8300)


def test_bind_config_env_overrides() -> None:
    from brick_console.run import bind_config

    assert bind_config({"BRICK_CONSOLE_HOST": "127.0.0.1"}) == ("127.0.0.1", 8300)
    assert bind_config({"BRICK_CONSOLE_PORT": "9001"}) == ("0.0.0.0", 9001)
    assert bind_config(
        {"BRICK_CONSOLE_HOST": "192.168.1.10", "BRICK_CONSOLE_PORT": "8301"}
    ) == ("192.168.1.10", 8301)


def test_bind_config_invalid_port_fails_fast() -> None:
    from brick_console.run import bind_config

    with pytest.raises(SystemExit, match="BRICK_CONSOLE_PORT"):
        bind_config({"BRICK_CONSOLE_PORT": "http"})


def test_run_module_wires_app_to_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    # The console script target is brick_console.run:main; main() must
    # resolve host/port from the env, wire the real store seam, and hand
    # uvicorn the built app — production must never serve with
    # app.state.store None (the WS-gateway deliverable consumes it).
    import brick_console.run as run_mod

    captured: dict[str, object] = {}

    def fake_uvicorn_run(app, *, host, port, log_config=None):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(run_mod.uvicorn, "run", fake_uvicorn_run, raising=True)
    monkeypatch.setenv("BRICK_CONSOLE_HOST", "127.0.0.1")
    monkeypatch.setenv("BRICK_CONSOLE_PORT", "9100")

    run_mod.main()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9100
    app = captured["app"]
    assert app.title == "brick-console"
    # The store seam is wired for real in production.
    assert isinstance(app.state.store, TelemetryStore)
    # /healthz reports the installed distribution's version, not a literal.
    assert app.state.version == run_mod._package_version()
    assert isinstance(app.state.version, str) and app.state.version


# ---------------------------------------------------------------------------
# StubManager — the manager production actually runs today
# ---------------------------------------------------------------------------


def test_stub_manager_parks_and_survives_two_lifespans() -> None:
    # Regression: an asyncio.Event created in StubManager.__init__ bound to
    # the first loop that awaited it; a second lifespan on the same app
    # (uvicorn --reload, a test re-entering a client) hit "Event is bound
    # to a different event loop" and silently killed the manager task.
    # The park latch must be per-run() so every lifespan gets its own loop.
    from brick_console.run import StubManager

    stub = StubManager()
    app = create_app(stub)

    with TestClient(app) as client:
        assert stub.started == 1
        body = client.get("/healthz").json()
    assert body["hub"] == {"state": "offline", "reason": "manager not wired yet (stub)"}

    # Second lifespan, fresh event loop: the stub must park again, not die.
    with TestClient(app) as client:
        assert stub.started == 2
        assert client.get("/healthz").json()["hub"]["state"] == "offline"
        assert app.state.manager_task is not None
        assert not app.state.manager_task.done()


def test_stub_manager_start_reason_matches_healthz() -> None:
    # The stub's contract: hub pinned offline, reason names the stub —
    # /healthz must surface exactly this until the real manager lands.
    from brick_console.run import StubManager

    stub = StubManager()
    with TestClient(create_app(stub)) as client:
        body = client.get("/healthz").json()

    assert body["hub"]["state"] == stub.state
    assert body["hub"]["reason"] == stub.state_reason == "manager not wired yet (stub)"


def test_bind_config_port_range_enforced() -> None:
    # Out-of-range ports fail fast with a clear message (the documented
    # promise), not an ugly socket error later at bind time.
    from brick_console.run import bind_config

    for bad in ("99999", "-1", "0"):
        with pytest.raises(SystemExit, match="1-65535"):
            bind_config({"BRICK_CONSOLE_PORT": bad})


def test_bind_config_rejects_padded_port_strings() -> None:
    # int() alone tolerates " 9" — strict parsing must not: a padded env
    # value is an operator typo, and port 9 would then fail at bind time
    # with an opaque permission error instead of a clear message.
    # Unicode digit characters ("²" parses as isdigit() but breaks int();
    # "٣" parses to 3) are rejected the same way: plain ASCII only.
    from brick_console.run import bind_config

    for bad in (" 9", "9 ", "  8300\t", "+8300", "²", "٣", "1_0"):
        with pytest.raises(SystemExit, match="1-65535"):
            bind_config({"BRICK_CONSOLE_PORT": bad})


def test_healthz_reads_manager_through_app_state() -> None:
    # The endpoint reads app.state.manager, not the factory closure: swap
    # the handle on the state object and /healthz must reflect the swap —
    # every consumer (endpoint, WS gateway) sees one source of truth.
    manager = FakeManager(state="offline", state_reason="a")
    app = create_app(manager)

    with TestClient(app) as client:
        before = client.get("/healthz").json()["hub"]
        app.state.manager = FakeManager(state="agent", state_reason="b")
        after = client.get("/healthz").json()["hub"]

    assert before == {"state": "offline", "reason": "a"}
    assert after == {"state": "agent", "reason": "b"}
