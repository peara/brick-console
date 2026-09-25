# pybricksdev library API — reference notes (Q1 spike)

**Audience:** brick-console server code, written for a human who won't read the library.
**Scope:** the installed `pybricksdev` 2.3.2 (with bleak 3.0.2), read from
`.venv/lib/python3.12/site-packages/pybricksdev/`. This is a read-only spike: no BLE was run.
**Verdict up front:** yes — scan-by-name, connect, download-and-run (to RAM), stop, stdin, stdout,
and hub status events are all exposed at library level. The one thing pybricksdev does *not* give us is a
connection-reuse layer (one `PybricksHubBLE` = one `BleakClient` = one connection; a fresh hub object
must be built per re-connect). See the [gotchas](#gotchas) and the `Transport` seam (`src/brick_console/transport.py`).

Path shorthand below: `connections/pybricks.py` =
`pybricksdev/connections/pybricks.py`, `ble/__init__.py` = `pybricksdev/ble/__init__.py`,
`compile.py` = `pybricksdev/compile.py`.

## Capability map

| # | Capability | How | Signature | Where | Notes |
|---|---|---|---|---|---|
| 1 | Discover hubs by name (scan) | `find_device()` module function, wraps `BleakScanner.find_device_by_filter` | `async def find_device(name: str \| None = None, service: str = PYBRICKS_SERVICE_UUID, timeout: float = 10) -> BLEDevice` | `ble/__init__.py:17-69` | Matches service UUID **and** local name (case-insensitive; also accepts a Bluetooth address as `name`). Raises `asyncio.TimeoutError` on no match — so "hub not advertising" surfaces as a clean, catchable timeout. Single device, not a list — for multi-hub later, use bleak's scanner directly with the same filter logic. |
| 2 | Connect | Build `PybricksHubBLE(device)` from a discovered `BLEDevice`, then `await hub.connect()` | `PybricksHubBLE.__init__(self, device: BLEDevice)` (`connections/pybricks.py:829`), `async def connect(self)` (`connections/pybricks.py:290`) | `connections/pybricks.py:825-887` | `connect()` is state-guarded: raises `RuntimeError` if already connected/connecting (`pybricks.py:291-294`). On connect it enables notifications (NUS TX + Pybricks command/event chars), reads firmware + protocol version, rejects an unsupported Pybricks protocol version with `RuntimeError` (`pybricks.py:860-866`), and reads hub capabilities into `hub._max_write_size`, `_capability_flags`, `_max_user_program_size`, `_num_of_slots` (`pybricks.py:871-878`). |
| 3 | Disconnect | `await hub.disconnect()` | `async def disconnect(self)` (`connections/pybricks.py:317-328`) | `connections/pybricks.py:317-328` | Idempotent — a debug log "skipping disconnect because not connected" if not connected. Spontaneous loss (hub off, button, out of range) arrives via bleak's `disconnected_callback` → `_handle_disconnect()` → `connection_state_observable` flips to `DISCONNECTED` (`pybricks.py:286-288, 834-839`). |
| 4 | Install program into hub RAM + start (the `run ble` flow, at library level) | `await hub.run(py_path, wait=False, ...)` — compiles `.py` → MPY, chunks it into user RAM, sets metadata, then `START_USER_PROGRAM` | `async def run(self, py_path: str \| None = None, wait: bool = True, print_output: bool = True, line_handler: bool = True) -> None` (`connections/pybricks.py:581-587`); `download()` `pybricks.py:537`, `download_user_program()` `pybricks.py:442`, `start_user_program()` `pybricks.py:500`, `stop_user_program()` `pybricks.py:527` | `connections/pybricks.py:442-629` | `run()` = `download(py_path)` → `start_user_program()` → optionally wait. RAM-only: `download_user_program` writes via `COMMAND_WRITE_USER_RAM` + `WRITE_USER_PROGRAM_META` — never touches the 5 permanent slots. **Precondition:** download/start are *rejected* with `CommandError.BUSY` (surfacing as a bleak GATT write failure) while a user program is running — there is no auto-stop; call `stop_user_program` first. Any mode transition into an install therefore sequences stop-before-install. Program-too-large fails early with `ValueError` (checked against `_max_user_program_size`, `pybricks.py:460-463`). `wait=True` blocks until the status flag `USER_PROGRAM_RUNNING` clears (`_wait_for_user_program_stop`, `pybricks.py:782-822`). For a server we want `wait=False` + our own state machine listening to `status_observable`. |
| 5 | Stop running program | `await hub.stop_user_program()` — writes `STOP_USER_PROGRAM` command | `async def stop_user_program(self) -> None` (`connections/pybricks.py:527-535`) | `connections/pybricks.py:527-535` | No-op for the hub if nothing is running (hub-side command semantics). Response write, so GATT errors surface as exceptions. **`stop_user_program` is the safe first move in any mode transition** — it clears the BUSY precondition on install/start (row 4) and is harmless when idle. |
| 6 | Receive stdout (Nordic UART / WRITE_STDOUT notifications) | Two channels: (a) **RxPY observable** `hub.stdout_observable` — raw stdout bytes; (b) **line queue** `hub.read_line()` — buffered lines when `line_handler=True` | `stdout_observable` property → `Observable[bytes]` (`connections/pybricks.py:174-179`); `async def read_line(self) -> str` (`connections/pybricks.py:423-440`); line splitting in `_handle_line_data` / `_line_handler` (`pybricks.py:181-249`) | `connections/pybricks.py:174-249, 269-284` | Notifications are dispatched by `_pybricks_service_handler` (`pybricks.py:269-284`): `STATUS_REPORT` → `status_observable` (32-bit `StatusFlag`), `WRITE_STDOUT` → `_stdout_subject`. On our v4.0.1 firmware (protocol ≥ 1.3) stdout comes as Pybricks events, **not** NUS; NUS handling is legacy-only. **Gotcha:** `_line_handler` auto-interprets `PB_OF:`/`_file_begin_` control lines by opening/closing files on the *server* filesystem (`pybricks.py:190-215`) — benign for us but worth knowing. Also note `print_output=True` prints hub output straight to the server's stdout (`pybricks.py:226-227`); the server should set it `False` and take the observable. |
| 7 | Write stdin (commands) | `hub.write_string()` / `hub.write_line()` (chunked to `_max_write_size - 1`) or low-level `hub.write()` (single packet) | `async def write(self, data: bytes) -> None` (`connections/pybricks.py:378-400`); `async def write_string(self, value: str) -> None` (`pybricks.py:402-411`); `async def write_line(self, value: str) -> None` (`pybricks.py:413-421`) | `connections/pybricks.py:378-421` | Modern firmware path is `Command.WRITE_STDIN` on the Pybricks characteristic with response-write (`pybricks.py:392-400`). `write()` raises `ValueError` if data exceeds one packet. Chunking beyond that is ours only via `write_string`. |
| 8 | Hub capabilities (max write size / chunking) | Read automatically at connect into `hub._max_write_size` etc.; program download chunks itself at `_max_write_size - 5` | Capabilities char unpacked by `unpack_hub_capabilities(data: bytes) -> tuple[int, HubCapabilityFlag, int, int]` — (max_char_size, flags, max_user_prog_size, num_of_slots) | `ble/pybricks.py:354-373` (unpack), `connections/pybricks.py:871-878` (read at connect), `connections/pybricks.py:473-491` (chunked download) | **Handled internally.** Program-download chunking is done by pybricksdev (`payload_size = _max_write_size - 5`, `pybricks.py:473-480`); stdin chunking via `write_string`. We never hand- chunk GATT writes for these flows. The `PybricksHub._max_write_size` default is 20 until connect populates it (`pybricks.py:90-96`). |
| 9 | Failures: disconnects, timeouts, hub busy | Exceptions + observables: `HubDisconnectError`, `HubPowerButtonPressedError` (both `RuntimeError` subclasses); `race_disconnect(awaitable)` races any op against disconnect; connection state is a `BehaviorSubject[ConnectionState]` | `HubDisconnectError` / `HubPowerButtonPressedError` (`connections/pybricks.py:68-73`); `async def race_disconnect(self, awaitable: Awaitable[T]) -> T` (`pybricks.py:330-376`); `connection_state_observable: BehaviorSubject[ConnectionState]` (`pybricks.py:130`); `ConnectionState` enum `connections/__init__.py:7-27` | `connections/pybricks.py:68-73, 130, 286-288, 330-376` | Disconnect mid-op → `HubDisconnectError("disconnected during operation")`. Hub busy → GATT attribute error `CommandError.BUSY` (0x81) on a write — surfaces as a bleak write failure (`ble/pybricks.py:168-173`). Scan/never-found → `asyncio.TimeoutError` (`ble/__init__.py:66-67`). Protocol mismatch at connect → `RuntimeError` (`pybricks.py:864`). Program too large → `ValueError` (`pybricks.py:460`). The status observable carries `USER_PROGRAM_RUNNING`, battery flags, `BLE_HOST_CONNECTED` etc. (`ble/pybricks.py:201-262`). |
| 10 | Gotchas | — | — | — | See below. |

## Lifecycle: who owns the asyncio tasks

`PybricksHubBLE` owns **no long-lived asyncio tasks**. All work is done inside the caller's
`await`ed coroutines: bleak's `BleakClient` internally manages its BlueZ D-Bus watchers, and
notifications arrive via callbacks scheduled on our event loop (`pybricks.py:837-839`,
`start_notify` → `pybricks.py:898-899`). There is no background reader task to cancel on our
side. The only internally-created task in the whole connection class is in the *USB* path
(`PybricksHubUSB._monitor_task`, `pybricks.py:1014`) — irrelevant over BLE.

So the server's ownership rule is simple: **we** own the event loop and every task we create; the
hub object is a passive coroutine-driven state holder. `connect()` guards against double-connect
via the `connection_state_observable` value (`pybricks.py:291-294`), and `disconnect()` is
idempotent.

**Object lifetime:** `PybricksHubBLE(device)` binds one `BleakClient` to one `BLEDevice`.
There is no public `close()` beyond `disconnect()`. To re-connect after a disconnect, build a
fresh `PybricksHubBLE` — reusing the same object raises `RuntimeError` ("attempting to connect
with invalid state" is only for non-DISCONNECTED, but a *new* connection on the same object is
safe only when state is DISCONNECTED... in practice the CLI itself builds a new hub object on
every reconnect loop, `cli/__init__.py:259-279` — we will follow that pattern; it's the proven one).

## The `pybricksdev run ble` flow at library level (what the CLI does)

`cli/__init__.py:185-247` (`Run.run`) is a thin wrapper. Library-level equivalent:

```python
from pybricksdev.ble import find_device
from pybricksdev.connections.pybricks import PybricksHubBLE

device = await find_device("Pybricks Hub")  # scan by name (ble/__init__.py:17)
hub = PybricksHubBLE(device)  # connections/pybricks.py:829
await hub.connect()  # pybricks.py:290
try:
    await hub.run("hello.py", wait=True)  # compile → RAM download → start → wait
finally:
    await hub.disconnect()  # pybricks.py:317
```

`run()` internally (`connections/pybricks.py:581-629`): checks connected state → resets output
buffers → `download(py_path)` (`pybricks.py:537`: capability check + `compile_multi_file` +
`download_user_program`) → `start_user_program()` (`pybricks.py:500`) → optionally
`_wait_for_user_program_stop()` (`pybricks.py:782`, watches `status_observable` for the
`USER_PROGRAM_RUNNING` flag edge with `race_disconnect` protection).

## Gotchas

1. **One BLE central at a time.** The hub accepts a single connection; a second central
   (Pybricks Code in a browser, another script) either fails to connect or kicks us off
   depending on firmware. pybricksdev has no arbitration — this is a server-side policy
   (AGENTS.md rule 3), enforced by our state machine, not the library.
2. **Address drift after re-flash.** `find_device(name)` matches on advertised name and
   returns a fresh `BLEDevice`; nothing in the library caches or depends on the address
   (`ble/__init__.py:44-69`). The CLI always re-scans on reconnect
   (`cli/__init__.py:266-270`) — do the same; never persist the address as an invariant.
3. **Pairing: not needed.** The Pybricks profile uses no authentication/encryption; the
   whole bring-up ran unpaired. `BleakClient(pair=...)` exists but pybricksdev never sets
   it (`connections/pybricks.py:837-839` passes only `disconnected_callback`). Keep it that way.
4. **`print_output` side effect.** `PybricksHub.__init__` sets `print_output = True`
   (`pybricks.py:134`) — by default every hub stdout line is `print()`ed to the server's
   stdout. A server embedding the hub object must set `hub.print_output = False` and consume
   `stdout_observable` (or `read_line()`) itself. Same for `_line_handler`'s `PB_OF:` /
   `_file_begin_` file-writing feature — if a user program prints those markers, files appear
   in `hub.script_dir`. Server use should keep `line_handler=False` in `run()` unless
   `read_line()` semantics are wanted, and treat output as raw bytes.
5. **`asyncio.TimeoutError` is the "not found" signal.** `find_device` raises it after
   `timeout` (default 10 s) — catch it, don't let it kill a supervisor loop. It is also the
   BlueZ connect-failure path in some cases; wrap `connect()` in `asyncio.wait_for` for a
   predictable bound (bleak's own default connect timeout is 30 s, `bleak/__init__.py:536`).
6. **tqdm progress bars in `download_user_program`.** The download writes a progress bar to
   stderr (via `tqdm.auto`, `pybricks.py:476-479`). In a headless server this is noise on
   stderr; acceptable, but worth knowing it isn't structured logging. There's no flag to
   disable it without reimplementing `download_user_program` — for the M1 dashboard the
   progress can be ignored or the method reimplemented with the same GATT writes.
7. **`system.storage` is 512 bytes** and cleared on firmware change (AGENTS.md "Hub runtime
   facts") — nothing in this library interacts with it; included to avoid hunting for it here.
8. **Slot writes are v1.4-protocol territory.** `start_user_program(slot)` accepts a slot id
   (`pybricks.py:500-525`) but *downloading into a slot* is not implemented at library level
   (only RAM download + start). That's BRD Q2's territory, unaffected by this spike.

## What is *not* exposed (and the workaround)

- **Reconnect on one hub object** — no. Build a new `PybricksHubBLE(BLEDevice)` per connection
  (CLI pattern, `cli/__init__.py:259-279`). The `Transport` implementation will therefore create
  a fresh hub per connect and translate its observables into our own event stream.
- **Multi-device discovery** — `find_device` returns the first match only. For 1-hub v1 this is
  fine; a future multi-hub feature would call `BleakScanner` directly with the same
  name/service filter from `ble/__init__.py:44-60`.
- **Slot programming** — see gotcha 8; out of scope (Q2).
- **Disabling the tqdm progress bar in `download_user_program`** — not exposed; tolerable noise,
  or reimplement the ~30 lines of chunked GATT writes using the same public constants.

## Transport seam (result of this spike)

The `Transport` abstract interface codifying these findings lives in
[`src/brick_console/transport.py`](../../src/brick_console/transport.py). Design decisions
traceable to these notes:

- `discover()` mirrors `find_device` (name + timeout, `asyncio.TimeoutError` on miss).
- `connect()` takes the discovered handle (a `BLEDevice` under the hood) — one fresh connection
  object per call, matching the CLI's proven reconnect pattern.
- `install_and_start()` combines compile + RAM download + start — the `hub.run(..., wait=False)`
  flow — deliberately *without* waiting for program completion: the state machine consumes
  `status_observable` edges instead (`USER_PROGRAM_RUNNING` flag). Precondition: download/start
  are rejected with `CommandError.BUSY` while a program runs — stop first.
- `subscribe_stdout()` is a callback API (not a queue) so the server can fan out to N WebSocket
  clients; pybricksdev's RxPY subjects map onto it naturally.
- `subscribe_status()` (added in the docs-review pass, review finding 1) maps
  `status_observable` onto a `StatusListener` callback carrying `StatusFlags` snapshots —
  the same `USER_PROGRAM_RUNNING` edges, without RxPY in server code.
- `write_stdin()` carries the chunking burden that `write_string` already solves internally.
- `disconnect()` idempotent, spontaneous disconnects surfaced via the connection-state callback.