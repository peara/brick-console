# Firmware archives

Firmware binaries are intentionally gitignored (`.zip`/`.bin`).

Current hub state: **Pybricks v4.0.1** (`pybricks-primehub-v4.0.1.zip`, from
[pybricks-micropython releases](https://github.com/pybricks/pybricks-micropython/releases)).

Original LEGO firmware backup (recovery path): stored outside the repo at
`~/hermes/m5stack/backups/lego-original-inventor-hub.bin`
(1,015,808 bytes, md5 `d0c76999d50b209881e16fe374982267`).

Re-flash / restore (hub in DFU mode first — hold Bluetooth button + plug USB):

```bash
uv run pybricksdev flash firmware/pybricks-primehub-v4.0.1.zip     # Pybricks
uv run pybricksdev dfu restore ~/hermes/m5stack/backups/lego-original-inventor-hub.bin  # LEGO
```