# D8 — Takeover detection: status-report-silence watchdog + canonical reason token

**Status:** accepted (2026-09-29; amended 2026-09-30 — liveness signal reworked from GATT-read probes to status-report silence, see the amendment at the end)

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
  without disturbing the holder. Takeover requires an open pairing window,
  and the BT button is a **no-op while a program runs** (observed
  2026-09-30) — so while the console's agent holds the hub, no new window
  can be opened; the user must power-press to stop the agent, then press
  BT in the ~5 s reinstall idle window. But a window opened while the hub
  was idle **stays open after a connect** (observed 23:05: the app kicked
  the console's live agent session through the still-open window ~30 s
  after the console connected) — so the likelier production takeover is
  the user pressing BT, the always-on console reconnecting through the
  window first, and the user then connecting from the app. Within pairing
  mode, last-central-wins kicks the incumbent silently.

**Decision:** takeover is detected by *signature*, not by event:

1. The manager's park loop (`_park_until_drop`) bounds the drop-event wait
   by `liveness_timeout` (default 5 s). Liveness is read from the hub's own
   status reports — the 2 Hz push channel the manager already subscribes
   to (`Transport.subscribe_status`, D6): every report timestamps
   `conn.last_status_at`, and silence past the timeout opens the grace
   window.
2. `liveness_grace` (default 2 s) adjudicates the silence: the disconnect
   callback arriving within it → ordinary `hub disconnected` (power-off
   shape — a final `BLE_HOST_CONNECTED=False` farewell arrives first).
   Fresh reports resuming → transient silence, park re-arms. Neither → the
   takeover path.
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

*Amended 2026-09-30:* the original decision used a benign GATT read
(`FW_REV_UUID`) as the periodic probe. Live-check counter-evidence killed
it: **a program start swaps the hub's GATT table**, so a read against a
cached characteristic object fails with `UnknownObject` — the kick's exact
error shape — while the link is fully alive (status reports and stdout
still streaming; hub BT light solid). The watchdog therefore mislabeled a
healthy AGENT session as takeover ~7 s in, tore the session down, the
teardown itself failed (BlueZ objects already gone), and the hub kept
holding the dead session: program running, never re-advertising — a wedge
requiring a hub power cycle to clear. Two such wedges were observed
(2026-09-29 23:51 and 2026-09-30 00:02) before the pattern was identified;
the discriminating evidence was the dashboard log streaming heartbeat
lines at the same moment the read-probe failed — the push channel was
truthful exactly where the pull channel lied. Liveness now reads the
status-report push channel itself (silence + no callback = the signature);
`Transport.probe()` was removed again (the seam stays at 8 operations — a
GATT-read liveness op is unsound on this firmware). The 2026-09-29 kick
evidence still carries the decision: at the real kick, status notifications
stopped mid-`True` and no callback ever fired; at power-off, the farewell
report and callback arrived within ~250 ms; at transient silence, reports
resume. A further implementation constraint surfaced by the rework's own
tests: the liveness timing trio (stamp, silence start, grace deadline) must
run on real `time.monotonic`, never an injected clock — a frozen fake clock
as a deadline source wedges the adjudication loop.