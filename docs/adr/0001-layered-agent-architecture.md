# ADR-0001: Build to The Agent Stack layer model

- Status: accepted
- Date: 2026-09-21

## Context

"Agent" is too coarse a unit to debug. When something fails, the diagnosis is "the
agent forgot" or "the agent hallucinated", which is the least useful thing anyone can
say at 3am. The Agent Stack series argues for named layers with distinct ownership so
that failures resolve to a layer.

## Decision

Nine code layers plus one documented substrate, as recorded in `STACK.md`, with
dependency direction enforced by `import-linter` contracts in `.importlinter`.

The six boundary confusions (session/authorization, transcript/context, memory/learning,
capability/execution, approval/isolation, observability/evaluation) are treated as
architectural invariants and given fitness tests in `tests/fitness/`.

## Consequences

- More modules than a single-file agent needs on day one. Accepted: the boundaries are
  what make the system survive retry, resume and prompt injection later, and they are
  far more expensive to introduce after feature code assumes their absence.
- A change that needs to blur a boundary must argue for it in an ADR, not in a diff.
- `.importlinter` and `tests/fitness/` become the load-bearing artifacts. Weakening
  either is a bar change, caught by `scripts/stack_guard.py`.
