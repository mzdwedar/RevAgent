# Spec template

`/spec` produces the six core areas from `spec-driven-development` **plus** the three
Agent Stack sections below. A spec missing the stack sections is not accepted: the
whole point is that layer ownership is decided before code, not discovered after.

---

# Spec: [Feature]

## Objective
## Tech Stack
## Commands
## Project Structure
## Code Style
## Testing Strategy

## Layer Ownership Ledger  *(Part 1)*

Which layers does this change touch, and what does it add to each?

| Layer | Touched? | What changes | Fitness test that proves it |
|---|---|---|---|
| 1 Interfaces | | | |
| 2 Control plane | | | |
| 3 Runtime / durability | | | |
| 4 Model engine | | | |
| 5 Context / memory | | | |
| 6 Tools | | | |
| 7 Execution surfaces | | | |
| 8 Identity / policy / approvals | | | |
| 9 Observability / evaluation | | | |
| 10 Infrastructure | | | |

## Foundation Assumptions  *(Part 2)*

Lower-layer constraints this feature inherits. State them; do not discover them.

- **Delivery semantics:** at-least-once / exactly-once / best-effort — and what that
  forces on idempotency
- **Consistency model:** what the store guarantees, and what the runtime may assume
- **Isolation & failure semantics:** what happens on restart, partition, timeout
- **Model asset:** which weights, context window, max output, modality
- **Serving system:** who serves it, queueing, batching, streaming, tail latency
- **Interaction contract:** which API shape, tool-call format, state management
- **Budgets:** context tokens, p95 latency, cost per turn — propagated upward

> Compatible is not equivalent. An OpenAI-compatible endpoint is not evidence of
> equivalent scheduler, cache or latency behavior. Benchmark with real request shapes.

## Boundary Decisions  *(Part 1's six confusions, answered explicitly)*

| Question | Decision |
|---|---|
| What owns session identity, and how is it distinct from authorization? | |
| What is the transcript store, and how is prompt context derived from it? | |
| What is memory here, who scopes it, and when is it written? | |
| Which capabilities are exposed, and which surface actually executes them? | |
| Where is the approval boundary, and what isolates the action afterwards? | |
| What evidence is emitted, and what criteria judge it? | |

## Boundaries

- **Always:** [...]
- **Ask first:** adding a dependency; changing a `.importlinter` contract; changing the
  durable-execution backend; widening a tool's scope
- **Never:** expose a tool that performs its own side effect; write memory implicitly;
  approve at task start for a later act; let untrusted content reach an authority decision
