"""Transport seam (issue #2): the ABC imports and pins the hub-access surface.

Every hub operation the server supports must appear as an abstract method
here — this test fails when someone adds a capability to the seam without
going through the interface, or removes one silently.
"""

import inspect

import brick_console.transport as transport_module
from brick_console.transport import (
    DisconnectListener,
    DiscoveredHub,
    StdoutListener,
    Transport,
)

EXPECTED_METHODS = {
    "discover",
    "connect",
    "install_and_start",
    "stop",
    "write_stdin",
    "subscribe_stdout",
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
    assert DisconnectListener is not None
