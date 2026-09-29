"""Transport seam (issue #2): the ABC imports and pins the hub-access surface.

Every hub operation the server supports must appear as an abstract method
here — this test fails when someone adds a capability to the seam without
going through the interface, or removes one silently.

The surface has 9 operations: `subscribe_status` was added because
program end is only observable through hub status events
(`USER_PROGRAM_RUNNING` flag edges), not stdout — a silent program exit
would otherwise be undetectable (m1-docs-review.md finding 1); `probe`
was added because a second-central takeover produces no disconnect
callback at all on this stack — a parked manager would otherwise wait
forever (#24 hardware evidence, 2026-09-29).
"""

import inspect

import brick_console.transport as transport_module
from brick_console.transport import (
    DisconnectListener,
    DiscoveredHub,
    StatusFlags,
    StatusListener,
    StdoutListener,
    Transport,
)

EXPECTED_METHODS = {
    "discover",
    "connect",
    "install_and_start",
    "stop",
    "probe",
    "write_stdin",
    "subscribe_stdout",
    "subscribe_status",
    "disconnect",
}


def test_transport_imports_cleanly() -> None:
    assert issubclass(Transport, inspect.getmro(Transport)[1])  # ABC subclass
    assert isinstance(transport_module.__all__, list)


def test_transport_pins_the_abstract_surface() -> None:
    abstracts = set(Transport.__abstractmethods__)
    assert abstracts == EXPECTED_METHODS
    for name in abstracts:
        assert inspect.iscoroutinefunction(getattr(Transport, name)), name


def test_transport_support_types_exist() -> None:
    # Protocol types re-exported for callers; nothing behavioral to assert.
    assert DiscoveredHub is not None
    assert StdoutListener is not None
    assert StatusListener is not None
    assert DisconnectListener is not None


def test_status_flags_match_pybricksdev() -> None:
    # StatusFlags must mirror pybricksdev's StatusFlag bit-for-bit: the
    # adapter passes the raw STATUS_REPORT flag word through unchanged
    # (pybricksdev decodes it as StatusFlag(struct.unpack_from("<I", ...))).
    # A wrong bit value would silently alias another hub flag — e.g. 0x01
    # is the battery low-voltage *warning* bit, not program-running.
    from pybricksdev.ble.pybricks import StatusFlag

    assert StatusFlags.USER_PROGRAM_RUNNING == StatusFlag.USER_PROGRAM_RUNNING
    assert StatusFlags.BLE_HOST_CONNECTED == StatusFlag.BLE_HOST_CONNECTED
    assert int(StatusFlags.USER_PROGRAM_RUNNING) == 1 << 6
    assert int(StatusFlags.BLE_HOST_CONNECTED) == 1 << 9
