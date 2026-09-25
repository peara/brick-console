## D5 — Server is the source of truth for programs; hub storage optional

**Status:** accepted (2026-09-23)

**Context:** The hub has 5 permanent program slots + RAM-run programs. Where should the program library live?

**Decision:** Programs live on the server filesystem (`programs/`), run-to-RAM by default. Permanent slots are a stretch (R9, blocked on Q2 — scriptable slot writes unverified).

**Consequences:** No sync problem, full CRUD over REST, hub power-cycles lose nothing. Hub stays stateless; button-only standalone boot of a chosen program is deferred with R9.
