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
- spontaneous disconnects must be observable so the state machine can react.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

__all__ = ["DisconnectListener", "DiscoveredHub", "StdoutListener", "Transport"]


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
        treats as "hub off/asleep" (AGENTS.md rule 5: no scan loops when the
        hub is off — report and stop).
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
        *started* — completion is observed through stdout/status
        subscriptions, keeping the server responsive. Raises if the program
        does not compile or exceeds the hub's RAM program size.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the currently running user program, if any.

        Exists because the user must be able to abort a runaway program from
        the web console. Safe to call when nothing is running (the hub-side
        stop command is idempotent).
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
    async def disconnect(self) -> None:
        """Disconnect from the hub, releasing the BLE central role.

        Exists because the hub accepts a single central — holding the
        connection blocks every other client (Pybricks Code included). Must
        be idempotent: spontaneous disconnects land in the same state as
        deliberate ones, and both end with the ``on_disconnect`` callback
        having fired.
        """
