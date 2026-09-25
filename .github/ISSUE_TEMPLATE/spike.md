---
name: Spike
description: Time-boxed investigation answering a question — read-only on the repo; findings go to docs/research/ or an ADR
labels: ["spike"]
body:
  - id: question
    type: textarea
    attributes:
      label: Question to answer
      description: One sharp question. A spike is done when it is answered, not when code exists.
    validations:
      required: true
  - id: method
    type: textarea
    attributes:
      label: How to investigate
      description: "Sources, files to read, experiments to run. Read-only on the repo: findings go into docs/research/ or an ADR, not src/."
    validations:
      required: true
  - id: done
    type: textarea
    attributes:
      label: Done when
      description: The deliverable is a documented answer. If it forces an architecture decision, the ADR lands in docs/adr/ + a row in the decisions.md index.
      value: |
        - [ ] Question answered in docs/research/<topic>.md (or inline here if small)
        - [ ] If architecture-level: ADR file in docs/adr/ + index row (decisions.md)
        - [ ] `uv run pytest` green / `uv run ruff check .` clean (only if repo files were touched)
  - id: notes
    type: textarea
    attributes:
      label: Notes
      value: |
        Hardware: the agent must never power on, flash, or touch the hub.
  - id: decisions
    type: textarea
    attributes:
      label: Decisions (settled in review)
      description: Filled by Hermes if this spike needs a refinement loop before dispatch. Leave empty until then.
      value: ""
---
