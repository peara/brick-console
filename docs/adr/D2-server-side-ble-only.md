## D2 — Server-side BLE only; no Web Bluetooth anywhere

**Status:** accepted (2026-09-23)

**Context:** Browser-side BLE (Web Bluetooth) is what pybricks-code, lego-control-center, and pybricks-hub-tester all use. But it runs in the *browser's* machine, so a laptop without its own radio — or with the hub out of its range — gets nothing, and there is no always-on manager when no browser is open.

**Decision:** The Linux box is the sole BLE central. Browsers talk to the server over HTTP/WebSocket. This is the architecture proven by brickrail (114★ train-automation project).

**Consequences:** Zero-BLE laptop experience and unattended auto-reconnect become possible. Cost: we build a custom UI instead of self-hosting existing Web-Bluetooth apps; their UX (esp. lego-control-center) serves as the reference.
