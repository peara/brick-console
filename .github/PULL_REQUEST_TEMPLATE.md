<!-- PR discipline: implementation lands via PR, owner merges. Branches: `issue-<N>` (implementation), `docs/<topic>`, `chore/<topic>`. -->

## What & why

<!-- One paragraph: what this changes and why. Reference the issue (`Closes #N`) or ADR (D-ID) if applicable. -->

Closes #N <!-- remove if none -->

## Done-when status

<!-- Copy the issue's Done-when checkboxes here and check them off as verified, or note deviations. -->

- [ ] `uv run pytest` green (hub tests auto-skip without `BRICK_CONSOLE_HUB_TESTS=1`)
- [ ] `uv run ruff check .` + `ruff format --check .` clean
- [ ] …

## Verification

<!-- How the changes were checked: commands run + results. If Hermes verified independently, note that. If a test/AC is not covered, say so — do not leave silently unchecked. -->

## Hardware

<!-- If this touches BLE/hub-adjacent code, confirm: no hub interaction in this PR's tests; hub tests stay gated behind BRICK_CONSOLE_HUB_TESTS=1. Otherwise delete this section. -->

- [ ] No hub hardware touched by this PR (agent, tests, or CI)