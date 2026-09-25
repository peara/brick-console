## D-FL — Firmware: Pybricks v4.0.1 stable; LEGO firmware backed up

**Status:** accepted (2026-09-24 — supersedes the draft's "pin v3.x" stance)

**Context:** The draft BRD planned to pin Pybricks v3.x because v4 was beta. Between drafting and execution, Pybricks v4.0.1 went stable (2026-06-24 release; docs.pybricks.com/en/stable now documents v4.0.0).

**Decision:** Flash v4.0.1 stable. Keep the DFU backup of the original LEGO firmware (`~/hermes/m5stack/backups/lego-original-inventor-hub.bin`, 1,015,808 B, md5 d0c76999…) as the recovery path; `pybricksdev dfu restore` reverts any time.

**Consequences:** We build on the current stable line (system.storage, v4 API surface). Upgrades are deliberate and changelog-checked. Note: pybricksdev's `dfu backup` requires system `dfu-util` (its built-in backup is a stub) — installed on the box.
