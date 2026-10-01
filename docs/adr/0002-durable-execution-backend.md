# ADR-0002: Durable execution backend

- Status: **closed** 2026-09-24: Temporal, decided in [ADR-0007](0007-temporal-spike-findings.md) and `SPEC-durable-runtime.md`
- Date: 2026-09-21

## Context

Durable execution: a loop can answer a turn; a durable workflow can survive time. This agent waits
for human approval and for external systems, so runs must survive restart and resume
from a recorded position without repeating committed side effects.

Retry, replay, resume and idempotency are four separate concepts. Retry without
idempotency duplicates side effects. Replay without step boundaries repeats recorded
work. Resume without a persisted cursor invents context. A queue is not a workflow: at
least-once delivery does not know that step four of seven already completed.

## Options

| Option | Gives us | Costs |
|---|---|---|
| **Temporal** | Event-history replay, workflow/activity split, retries, signals for approval waits | Server to run; determinism discipline in workflow code |
| **LangGraph** | Checkpointer, `interrupt()` for approval waits, threads for continuity | Python-process-shaped; durability depends on checkpointer choice |
| **Postgres + steps/outbox** | Full control, no new infrastructure | We own replay, retries, wait wake-up and the outbox |

## Decision

Deferred. `agentstack.runtime` is written against the *invariants*, not a vendor:
stable `run_id`, side effects only inside a recorded step, waits persisted as state,
resume driven by an event carrying run id + pending wait + state snapshot.

`tests/fitness/test_run_identity.py`, `test_idempotency.py` and `test_waiting_is_state.py`
assert those invariants against the in-memory reference implementation, so whichever
backend is chosen has to satisfy them rather than replace them.

## Consequences

- The in-memory step ledger and wait store are a reference implementation, not the
  production one. They must be swapped before first deploy.
- Whichever backend wins, the fitness tests are re-pointed at it and must stay green.
