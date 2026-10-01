# ADR-0007: Temporal as the durable-execution backend — spike findings

- Status: **accepted** 2026-09-24, hybrid shape as recommended below (`SPEC-durable-runtime.md`)
- Date: 2026-09-24
- Layer: 3 (runtime)
- Follows: ADR-0002 (durable execution backend), ADR-0006 (LangGraph turn execution)

## What was verified, not assumed

As with ADR-0006, every claim below was checked against the installed package, not
memory: `temporalio` 1.33.0 (spike-only; since removed), Temporal CLI 1.9.1 / Server
1.32.0 dev server, `langgraph` 1.2.12. Probe code was removed after the decision (recoverable at `c48553c`, `spikes/temporal/`); the run log is
`docs/evidence/temporal/RESULTS.txt`. Each probe asserts an outcome and exits non-zero if it fails.

| Probe | Question | Result | What we have today |
|---|---|---|---|
| P1 | Does the server give us one run per id? | 5 concurrent starts on one workflow id: **1 started, 4 refused**. `USE_EXISTING` attaches a redelivery to the running one. With `REJECT_DUPLICATE`, a closed id refuses a late redelivery. | `runtime/cycles.py` claim |
| P2 | Are activities exactly-once? | **No.** An activity whose write landed but failed to report completion is retried, and the effect landed **2x**. With our idempotency key it landed **1x**. | `execution.gateway` + `idempotency_claims` |
| P3 | Can an approval wait refuse bad answers? | An **Update validator** refused both a stale answer (to a question already re-asked) and a second answer. Refused updates **leave no trace in history**. The `wait_condition(timeout=)` loop re-asked 3x with no silent expiry. | `runtime/waits.py`, `runtime/deadlines.py` |
| P4 | Does a parked run survive process death? | Worker SIGKILLed while parked. A fresh process resumed it, and `prepare`/`ask`/`commit` each ran **once**. | T17 resume path |
| P5 | Can CI catch a deploy that strands live runs? | `Replayer` against a recorded history **rejects** an unguarded inserted step (`TMPRL1100 Nondeterminism`) and **accepts** the same change behind `workflow.patched()`. Runs offline. | T19 checkpoint shape, T20 `checkpoint_guard.py` |
| P6 | Can the LangGraph turn run on Temporal? | `temporalio.contrib.langgraph` runs each node as an activity, with **no Postgres checkpointer**. `interrupt()` is resumed with `Command(resume=…)` from workflow state. | ADR-0006 graph + Postgres checkpointer |
| P7 | Is bounded fan-out built in? | 10 scorings with `max_concurrent_activities=2` peaked at **2**. | `runtime/fanout.py` (T21) |
| P8 | Can deadline tests skip time? | 3 re-asks across 72 simulated hours in **<4s** of wall-clock time. | real-clock deadline tests |

## Findings that change the design

### 1. Temporal does not replace the idempotency ledger (P2)
Activities run at least once. Temporal records that an activity *completed*, but
it can't know that a charge landed before a worker died. `gateway.execute` with an
idempotency key stays the only thing between a retry and a double side effect.
Adopting Temporal **removes none of Layer 7**.

### 2. ADR-0006's interrupt rule still binds (P6)
In the plugin, the node that calls `interrupt()` runs **twice**: once to interrupt, and
once more as a fresh activity on resume (`turn.approve` was scheduled 2x). The rule "a node
that interrupts does nothing before it interrupts" carries over unchanged.

### 3. The LangGraph plugin is experimental
The README says so. It also doesn't support LangGraph `Store`, and it requires every
node's definition to be importable from a named module (not `__main__`). Relying on it
for the turn means relying on an experimental API for Layer 3's core path.

### 4. Resume after a worker death is not instant (P4)
The first Update after the SIGKILL took **~10s**. The workflow task sits on the dead
worker's sticky queue until `sticky_queue_schedule_to_start_timeout` (default 10s)
expires. That's acceptable for a human approval, but it is a latency floor to record. The
timeout is configurable.

### 5. Determinism is enforced at import time
The workflow sandbox re-imports workflow modules and refused one that called
`shutil.which` at import. In practice, workflow modules must not import I/O modules.
That means `import-linter` would need a new contract: workflow modules may import
`agentstack.runtime` types, but never `agentstack.storage`/`execution`.

## What Temporal would own, and what stays ours

| Temporal owns (if adopted) | Stays ours |
|---|---|
| Run identity and start dedupe (P1) | Idempotency of external effects: Layer 7 gateway (P2) |
| Waits, timers, re-ask deadlines (P3, P8) | **Who may approve**, bound to fingerprint + snapshot: Layer 8. An Update validator checks shape and state only, never authority. |
| Resume after process death (P4) | The audit sink (Layer 9). Temporal history is not the audit record. |
| Deploy safety via patching + Replayer (P5) | Tenant scoping, and treating update/signal payloads as untrusted input |
| Bounded fan-out (P7) | Memory (Layer 5) |

## Costs

- **A new server** in dev (`docker-compose.yml`) and in production (self-hosted or Temporal
  Cloud). `tests/infra` would have to *fail* without it, not skip.
- **Two histories.** Temporal event history alongside Postgres `approvals`,
  `idempotency_claims` and `audit`. The boundary has to be explicit.
- **Migration of in-flight runs.** Runs parked in `waits` today have to drain or be
  migrated. `checkpoint_guard.py` logic moves to a Replayer gate.
- **Re-pointing the fitness tests.** `test_run_identity.py`, `test_idempotency.py` and
  `test_waiting_is_state.py` must target the Temporal-backed runtime and stay green.

## Recommendation (for decision, not decided)

**Adopt Temporal as the run owner, hybrid shape.** Temporal workflows own run identity,
waits, timers, re-ask, fan-out bounds and deploy-safety replay. These are the parts of
`agentstack.runtime` (T17–T21) we currently maintain by hand, and P1/P3/P4/P5/P7/P8 show
each of them works out of the box. The gateway, idempotency ledger, approvals policy and
audit stay in Postgres unchanged.

For the turn, **keep ADR-0006 and run the LangGraph turn inside a single activity**
for now rather than adopting the experimental plugin. Revisit the plugin once it leaves
experimental status, when the Postgres checkpointer could be retired.

If accepted, this goes through `/spec` → `/plan`. Every task names Layer 3 and the fitness
test that proves it, and adding `temporalio` as a runtime dependency is its own ask.
