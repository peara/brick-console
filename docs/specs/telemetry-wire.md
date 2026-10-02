# Telemetry wire spec

**Status:** living spec — extracted from [D7](../adr/D7-telemetry-wire-schema.md) per its evolution note (the first field-dictionary change), amended by [D9](../adr/D9-lean-idle-agent.md) (reading-mode tags, passive idle agent). The ADRs remain the decision records; the wire contract lives here, in one place. Hub-side encoder: `agent/brick_telemetry.py` (library) + `agent/agent_main.py` (idle-agent policy). Server-side model: `brick_console.events` (`_PORT_SPECS` is this spec's executable mirror). Browser mirror: `PORT_FIELDS` in `src/brick_console/static/dashboard.mjs`.

**The three copies.** This document, `events._PORT_SPECS`, and `dashboard.mjs PORT_FIELDS` are one contract in three places — any field change lands in all three, pinned by tests on the server and hub sides.

## The wire

UTF-8 JSON lines over hub stdout, one JSON object per `print()`, **CRLF-terminated** (MicroPython's line ending). Compact separators (`,`/`:`). Short wire keys throughout; the Python side uses full-word names (`"v"` → `voltage_mv`). No wire timestamp (the hub has no wall clock), no sequence number — the parser stamps `received_at` at successful decode; ordering is per-connection. A line may split at arbitrary byte boundaries across stdout notification chunks; the parser buffers partials with a hard cap (4,096 bytes).

## Event kinds

| Kind | Cadence | Keys |
|---|---|---|
| `hub_info` | once per connect (snapshot event — cached server-side, replayed before live stream) | `name` (BLE name) · `fw` (firmware string) · `model` (literal `"technichub"`) |
| `battery` | ~1 Hz | `v` mV · `c` mA · `pct` 0–100 — pct **derived hub-side** from `v` (a 2S Li-ion discharge curve; Pybricks v4 exposes no battery-percent API; approximate under load, raw mV/mA always present) |
| `imu` | ~10 Hz | `ax`/`ay`/`az` **mm/s²** (TechnicHub mounting +Z top, +X front; ≈ +9810 up at rest) · `gx`/`gy`/`gz` °/s (right-hand rule) · `up` side string (top/bottom/left/right/front/back) |
| `port` | ~10 Hz, one line per attached device, unconditional per cycle; `dev:"none"` **only on a detach transition** | `p` (A–F) · `dev` · optional `mode` (below) · per-device fields |

The kind key is `"t"` — reserved exclusively for the telemetry event kind inside WS telemetry envelopes too (D7 envelope rule).

## Reading-mode tags (D9)

Telemetry is not intrinsically passive — **the idle agent is.** Mode-dependent sensor lines carry a `"mode"` key asserting which reading the values belong to; the publisher asserts it because it must: the LUMP driver tracks the current mode, but Pybricks v4.0.1's Python API cannot read it — the one who drives the sensor is the only one who can declare what was read.

Two regimes share one wire:

- **Idle agent (AGENT mode)** — a passive guest: each sensor rests in its passive mode (parked at attach; every subsequent read same-mode, 0 ms), and the agent emits only the passive tags. The idle robot sits dark and silent; no illumination, no ping, no exceptions.
- **Programs (PROGRAM mode, M2 opt-in — BRD R5, issue #36)** — own the hub, may drive sensors actively, publish their active readings under the matching tags.

Tag values: ColorSensor `"ambient"` (light off) / `"surface"` (light on); UltrasonicSensor `"presence"` (transmitter off) / `"distance"` (active ping); ColorDistanceSensor `"ambient"` / `"distance"` / `"surface"`. **Single-mode devices carry no tag** (Motor, ForceSensor, TiltSensor, InfraredSensor, `none`) — and a mode-dependent device's line without a tag is malformed (rejected by the decoder, skipped whole by the dashboard).

The `InfraredSensor` stays in the dictionary (single-mode `d`, % relative — WeDo motion sensor) but is **never read by the idle agent**: it is an active IR emitter (LEGO-documented, 7 kHz pulsed) with no passive mode, so the idle agent neither detects nor reads it; a plugged IR sensor is invisible on the agent-mode dashboard until a program publishes it.

## Field dictionary per (device, mode)

`none` is the empty port (no keys). `dev` strings form an **open set** — the parser keeps `dev` a plain passthrough string, never a closed enum.

| Device | Mode | Fields (canonical emission order) | Passive? |
|---|---|---|---|
| Motor | *(no tag)* | `angle` ° (output shaft) · `speed` °/s (100 ms window) · `load` **mNm** (torque estimate, not %) | all passive |
| ColorSensor | `ambient` | `amb` % | **idle agent's line** |
| ColorSensor | `surface` | `refl` % · `h` 0–360° · `s` % · `v` % · `col` color name or `"none"` | program mode (M2) |
| ForceSensor | *(no tag)* | `f` N (~0–10) · `d` mm (~0–8) · `pressed` bool (3 N threshold) | all passive |
| UltrasonicSensor | `presence` | `pr` bool | **idle agent's line** |
| UltrasonicSensor | `distance` | `d` mm (2000 = no echo) | program mode (M2) |
| ColorDistanceSensor | `ambient` | `amb` % | **idle agent's line** |
| ColorDistanceSensor | `distance` | `d` **%** — the BOOST-era unit trap, not mm | program mode (M2) |
| ColorDistanceSensor | `surface` | `refl` % · `h`/`s`/`v` · `col` | program mode (M2) |
| TiltSensor | *(no tag)* | `pitch` ° · `roll` ° | all passive |
| InfraredSensor | *(no tag)* | `d` % relative | program mode only (active IR emitter — see above) |

Unit traps (pinned by tests on both sides): ColorDistanceSensor `d` is **percent**, not mm; Motor `load` is **mNm**, not a percentage; UltrasonicSensor `d` = 2000 renders as "no echo" in the dashboard.

## Canonical lines

Idle agent (D9: passive subset only; CRLF omitted):

```json
{"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}
{"t":"battery","v":8085,"c":42,"pct":87}
{"t":"imu","ax":120,"ay":-980,"az":9810,"gx":0,"gy":0,"gz":3,"up":"top"}
{"t":"port","p":"A","dev":"Motor","angle":12,"speed":0,"load":0}
{"t":"port","p":"B","dev":"ColorSensor","mode":"ambient","amb":12}
{"t":"port","p":"C","dev":"ForceSensor","f":0.0,"d":0.0,"pressed":false}
{"t":"port","p":"D","dev":"UltrasonicSensor","mode":"presence","pr":false}
{"t":"port","p":"E","dev":"TiltSensor","pitch":3,"roll":-2}
{"t":"port","p":"F","dev":"none"}
```

Program mode (M2 opt-in — the active readings, published by the driving program):

```json
{"t":"port","p":"B","dev":"ColorSensor","mode":"surface","refl":34,"h":10,"s":80,"v":90,"col":"red"}
{"t":"port","p":"D","dev":"UltrasonicSensor","mode":"distance","d":245}
```

## Validation policy

- A known `(dev, mode)` pair's listed keys are all **required** — a missing key is malformed (D7: emission is unconditional per cycle, so a missing key is an agent bug surfaced, counted, skipped; decoding never guesses).
- **Extra keys are ignored** (forward compatibility is versioned by addition — new kinds and new fields on known kinds are safely ignorable; the raw line stays the verbatim record).
- **Unknown `dev` strings and unknown `(dev, mode)` pairs pass through** — no typed fields decoded, the tag (if any) kept; never an error.
- A mode-dependent device's line **without a string `mode` tag is malformed** — the fields cannot say which reading they belong to.
- Wire-level failures — invalid JSON/UTF-8, a non-object payload, a missing or non-string `"t"`, wrongly-typed fields — raise `EventDecodeError` server-side (counted, skipped); the dashboard skips such envelopes whole (never a partial card).
- Numbers keep their JSON type (`12` stays `int`, never `12.0`) so canonical lines re-encode byte-exactly.