# D8 — Takeover detection: liveness probe + canonical reason token

**Status:** accepted (2026-09-29 — resolves the #24 evidence question, F6)

**Context:** F6 requires the dashboard to show EXTERNAL when another BLE
central takes the hub. The M1 dashboard overlay (PR #23) derives it from the
state envelope's `reason` — but the originally assumed design, "emit a
reason token on the disconnect path", assumed a disconnect *event* exists on
the takeover path. Hardware observation (2026-09-29, harness
`scripts/takeover_observation.py`, evidence in issue #24) proved it does not:

- **Kick path (pairing-mode takeover):** BlueZ removes the hub's D-Bus object
  tree (in-flight GATT reads fail with `org.freedesktop.DBus.Error.UnknownObject`)
  without ever flipping `Connected: false` — bleak's `disconnected_callback`
  never fires, and pybricksdev's observable never reports DISCONNECTED. A
  manager parked on the drop latch would hang in AGENT forever.
- **Power-off path (control):** a final `STATUS_REPORT` with
  `BLE_HOST_CONNECTED=False` arrives, then the disconnect callback fires
  within ~250 ms. The existing drop path works exactly as designed.
- **Held-hub facts:** a held hub never advertises in normal mode (invisible
  to scanners); a second-central connect is refused (30 s BlueZ timeout)
  without disturbing the holder. Takeover requires pairing mode — a
  deliberate physical act (BT button) — and the incumbent is then kicked
  (last-central-wins), silently.

**Decision:** takeover is detected by *signature*, not by event:

1. The manager's park loop (`_park_until_drop`) bounds the drop-event wait
   by `probe_interval` (default 5 s) and runs one benign liveness probe per
   tick — a single `FW_REV_UUID` characteristic read (`Transport.probe()`,
   the seam's 9th operation, D6 amended).
2. A failed probe opens a `probe_grace` window (default 2 s) for the
   disconnect callback to still arrive. Callback within it → ordinary
   `hub disconnected` (power-off shape). Silence past it → the takeover
   path.
3. The takeover path ends the session with the canonical reason token
   `EXTERNAL_TAKEOVER_REASON` = `"external client took the hub"` — the
   dashboard's existing `EXTERNAL_REASON_TOKENS` matches it by substring
   (`external`, `took the hub`), so the overlay lights up with **zero
   client changes**. The token is pinned by unit test on both sides; the
   WS gateway forwards `state_reason` verbatim, making the string wire
   contract.
4. After the token, the normal OFFLINE machinery takes over: rescan misses
   (held hub not advertising) produce non-transition notes, the client's
   sticky-within-offline-episode logic keeps the overlay lit, and release
   (hub re-advertises) auto-reconnects → AGENT → overlay clears. The F6
   flow end-to-end.

**Consequences:** the hang-on-kick defect is fixed as a side effect — the
manager can no longer park forever on a dead link. Detection is evidence-
driven (probe failure + callback silence), so it does not depend on the
kick's exception type; any link death the callback reports stays on the
ordinary path. Costs: one benign GATT read every 5 s while parked (proven
benign — the harness ran it at 2 Hz for minutes), and a false-label risk
for abnormal power deaths that skip the callback (graceful power-off fires
it, observed; a hard crash that skips it would be labeled takeover — rare,
self-correcting on reconnect, documented here). Server restart during a
held period loses the takeover reason (fresh offline episode, no overlay)
— acceptable for M1, noted as a limitation.

**Alternatives rejected:** *reason-on-disconnect* — no disconnect event
exists on the kick path (the observation that motivated this ADR).
*Advertising-visibility inference* — a held hub is invisible exactly like a
powered-off hub, so rescan results cannot separate the two; only the probe
signature can. *Reconnect-attempt probing* — attempting connects while
another central holds the hub is fighting (F6 forbids) and gets refused
without holder disturbance, yielding nothing.