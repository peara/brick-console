## D-GH — Repository: `peara/brick-console`, private, docs split

**Status:** accepted (2026-09-24)

**Context:** Work outgrew chat-driven notes; a real repo was wanted before M1 coding. Naming: the "hubdock" working title was never loved.

**Decision:** Private GitHub repo `brick-console`. Docs split into brd.md / architecture.md / decisions.md / research archive (frozen v0.1 draft + investigation). Repo is the code home from day one (uv env lives here; `.venv`, firmware binaries, and the LEGO backup are gitignored or kept outside).

**Consequences:** "hubdock" is retired; all references renamed to brick-console / `brick_telemetry`. Task tracking planned as GitHub Issues + milestones mirroring M0–M5 (Notion stays for diary/logs, not project tasks).
