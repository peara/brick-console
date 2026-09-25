---
name: Task
description: Implementation unit — dispatched to a coding agent (opencode) after decisions are settled
labels: []
body:
  - id: why
    type: textarea
    attributes:
      label: Why
      description: What problem this solves and who hits it. Link docs (BRD/architecture/ADR) where relevant.
    validations:
      required: true
  - id: what
    type: textarea
    attributes:
      label: What to build
      description: Scope boundary. Name files/modules. Reference ADRs by ID (D6, D7) and prior issues (#N).
    validations:
      required: true
  - id: done
    type: textarea
    attributes:
      label: Done when
      description: Acceptance criteria as checkboxes — machine-checkable without the hub. Tests/lint must appear as an item.
      value: |
        - [ ] `uv run pytest` green (hub tests auto-skip without `BRICK_CONSOLE_HUB_TESTS=1`)
        - [ ] `uv run ruff check .` + `ruff format --check .` clean
        - [ ] …
    validations:
      required: true
  - id: notes
    type: textarea
    attributes:
      label: Notes
      description: References, pitfalls, constraints. Always include the hardware line below (edit only if more is needed).
      value: |
        Hardware: the agent must never power on, flash, or touch the hub. All hub-dependent tests stay gated behind `BRICK_CONSOLE_HUB_TESTS=1` and are skipped in CI/agent runs.
  - id: decisions
    type: textarea
    attributes:
      label: Decisions (settled in review)
      description: Filled in by Hermes during the refinement loop — analysis + owner decisions land here so the issue is self-contained for full-auto dispatch. Leave empty until then.
      value: ""
---
