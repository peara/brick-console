"""The Transport seam: the only interface through which the server touches the hub.

Every bit of hub access in brick-console — discovery, connection, program
install/run, stdio, telemetry — flows through :class:`Transport`. The rest of
the server (state machine, WebSocket gateway, REST handlers) depends on this
abstract interface only, never on bleak or pybricksdev directly.

The interface is shaped by what the pybricksdev library actually exposes
(`docs/research/pybricksdev-api-notes.md`, BRD Q1):

- scan-by-name with a timeout, raising ``asyncio.TimeoutError`` on miss —
  mirrors ``pybricksdev.ble.find_device``;
- one connection object per connect (pybricksdev has no reconnect-on-one-object
  API; the CLI itself builds a fresh hub per connection);
- install-and-start is a RAM-only download — it never touches the hub's
  permanent slots (AGENTS.md rule 6);
- stdout arrives as push notifications (callback), not a pullable stream;
- program lifecycle is observable through status events, not stdout — a
  program that ends without printing would otherwise be undetectable
  (m1-docs-review.md finding 1);
- spontaneous disconnects must be observable so the state machine can react.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import IntFlag
from pathlib import Path
from typing import Protocol

__all__ = [
    "DisconnectListener",
    "DiscoveredHub",
    "StatusFlags",
    "StatusListener",
    "StdoutListener",
    "Transport",
]


class DiscoveredHub(Protocol):
    """Opaque handle to a hub found by :meth:`Transport.discover`.

    The concrete implementation wraps the discovered device (a bleak
    ``BLEDevice`` today). The server only ever passes it back to
    :meth:`Transport.connect` — it must not leak BLE types into server code,
    keeping the seam narrow.
    """

    @property
    def name(self) -> str:
        """Advertised device name (e.g. ``"Pybricks Hub"``)."""
        ...

    @property
    def address(self) -> str:
        """Bluetooth address; informational only — may drift after a re-flash."""


class StdoutListener(Protocol):
    """Receiver of raw stdout bytes pushed by the hub.

    Bytes, not lines: line splitting, JSON-lines telemetry parsing, and
    fan-out to WebSocket clients are server-side concerns (decisions D1/D4),
    so the transport hands over raw payloads only.
    """

    def __call__(self, data: bytes) -> None:
        """Called once per notification with the raw stdout payload."""
        ...


DisconnectListener = Callable[[], None]
"""Notified when the hub connection drops — deliberate or spontaneous."""


class StatusFlags(IntFlag):
    """Hub status bits, mirroring the Pybricks GATT ``STATUS_REPORT`` payload.

    Values match ``pybricksdev.ble.pybricks.StatusFlag`` bit-for-bit — the
    adapter passes the raw 32-bit flag word through unchanged — and are
    pinned by test against the installed library. Only the bits
    brick-console consumes are named; the hub reports more, so test
    specific bits (``flags & StatusFlags.USER_PROGRAM_RUNNING``), never
    plain truthiness.
    """

    USER_PROGRAM_RUNNING = 1 << 6
    BLE_HOST_CONNECTED = 1 << 9


StatusListener = Callable[[StatusFlags], None]
"""Notified on every hub status report (a snapshot of current flags, not an edge).

Consumers derive edges themselves (e.g. ``USER_PROGRAM_RUNNING`` clearing
means the user program ended — the only reliable program-end signal; stdout
cannot prove it, since a program may exit without printing)."""


class Transport(ABC):
    """Abstract BLE transport to the hub.

    An instance is the server's *capability* holder (adapter + policy), not a
    single connection: the lifecycle is discover → connect (now bound) →
    I/O → disconnect, mirroring how pybricksdev's own CLI drives its hub
    object. All methods run on the server's single asyncio event loop; none
    block.
    """

    @abstractmethod
    async def discover(self, name: str, *, timeout: float = 10.0) -> DiscoveredHub:
        """Scan for a hub advertising ``name``; resolve within ``timeout`` seconds.

        Exists because the hub's Bluetooth address drifts after re-flashes —
        the advertised name is the only stable selector. Raises
        ``asyncio.TimeoutError`` if no hub is found in time, which the caller
        treats as "hub off/asleep" (AGENTS.md rule 5: no *interactive* scan
        loops when the hub is off — report and stop; the console service's
        own bounded rescan loop is the sanctioned exemption).
        """

    @abstractmethod
    async def connect(
        self, hub: DiscoveredHub, *, on_disconnect: DisconnectListener
    ) -> None:
        """Connect to a hub previously returned by :meth:`discover`.

        Exists because the hub accepts exactly one BLE central; connecting is
        an explicit, state-changing step the state machine must authorize.
        The implementation performs the full handshake (service discovery,
        notification enable, capabilities read) before resolving, so a
        successful return means "ready for I/O".

        ``on_disconnect`` is invoked exactly once when the connection drops
        for any reason — deliberate :meth:`disconnect`, hub powered off, out
        of range, or another central stealing the hub. pybricksdev surfaces
        this via an observable state subject; here it is a plain callback so
        the state machine can react without RxPY.
        """

    @abstractmethod
    async def install_and_start(self, program: Path, *, wait: bool = False) -> None:
        """Compile ``program``, download it into hub RAM, and start it.

        Exists because this is the console's core flow (install + run a
        program from the ``programs/`` library). RAM-only by design: the
        hub's 5 permanent slots stay untouched (AGENTS.md rule 6, BRD Q2
        open). With ``wait=False`` the call resolves when the program has
        *started* — completion is observed through the status subscription
        (``USER_PROGRAM_RUNNING`` flag clearing), not stdout. Raises if the
        program does not compile or exceeds the hub's RAM program size.

        The hub rejects program writes with ``CommandError.BUSY`` while a
        user program is running — callers must :meth:`stop` first; the
        state machine sequences stop-before-install. BUSY surfaces as a
        GATT write error from the adapter (see api-notes row 4); it is a
        caller-side precondition, not a typed seam error.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the currently running user program, if any.

        Exists because the user must be able to abort a runaway program from
        the web console. Safe to call when nothing is running (the hub-side
        stop command is idempotent).
        """

    @abstractmethod
    async def probe(self) -> None:
        """Benign liveness check on the connected hub; resolves while the
        link is up, raises while it is gone.

        Exists because an external central taking the hub (F6) kills the
        link in a way this stack's disconnect callback never reports:
        hardware observation (2026-09-29, issue #24 evidence) showed
        BlueZ removes the hub's GATT objects with no ``Connected: false``
        property change, so ``on_disconnect`` never fires and a parked
        manager would wait forever. A periodic read distinguishes the
        paths — power-off fires the callback (a final
        ``BLE_HOST_CONNECTED=False`` farewell arrives first), takeover
        does not; probe failure plus callback silence within a grace
        window is the takeover signature. Must be side-effect free (no
        program writes, no slot traffic — a plain characteristic read).
        """

    @abstractmethod
    async def write_stdin(self, data: bytes) -> None:
        """Send ``data`` to the running program's stdin.

        Exists because programs accept interactive commands (M3: control
        channel). The implementation owns chunking to the hub's negotiated
        max write size — callers must not need to know BLE packet limits.
        """

    @abstractmethod
    async def subscribe_stdout(self, listener: StdoutListener) -> None:
        """Register ``listener`` for every raw stdout payload the hub pushes.

        Exists because stdout is push-only over BLE — there is no pullable
        stream — and the server needs to fan each payload out to WebSocket
        clients and the telemetry parser. Callback-based (not a queue) so
        multiple consumers can observe the same bytes. Idempotent per
        listener; an unsubscribe facility is added when a consumer needs it.
        """

    @abstractmethod
    async def subscribe_status(self, listener: StatusListener) -> None:
        """Register ``listener`` for hub status reports.

        Exists because program lifecycle is not observable through stdout:
        a user program that ends without printing leaves no stdout trace,
        and the only reliable program-end signal is the
        ``USER_PROGRAM_RUNNING`` flag clearing in a status report
        (m1-docs-review.md finding 1; pybricksdev's own
        ``_wait_for_user_program_stop`` watches the same flag). On subscribe
        the listener is invoked once with the current flags, then on every
        subsequent report (snapshot semantics — BehaviorSubject-style, so a
        late subscriber cannot miss the current state); consumers derive
        edges (set → running, clear → ended). Idempotent per listener,
        mirroring :meth:`subscribe_stdout`.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the hub, releasing the BLE central role.

        Exists because the hub accepts a single central — holding the
        connection blocks every other client (Pybricks Code included). Must
        be idempotent: spontaneous disconnects land in the same state as
        deliberate ones, and both end with the ``on_disconnect`` callback
        having fired.
        """
