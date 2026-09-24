# Spec: Durable runtime on Temporal

Status: **approved** (revision 1), 2026-09-24
Date: 2026-09-24
Parent: `SPEC.md` (Experiment Operator, revision 10). This is a sub-spec of the same one
capability, following the `SPEC-registry.md` precedent. It changes *how* the run is
orchestrated, not *what* the run does. Every success criterion in `SPEC.md` still holds,
and none is weakened here.

Evidence: ADR-0007 (what Temporal gives, 8 probes) and ADR-0008 (what enforcement needs
under it, 8 probes + a 480-case policy differential). Claims marked **(E1)**, **(P4)** and so
on point to those probes.

---

## Assumptions

Decided by you on 2026-09-24:

1. **Scope:** adopt Temporal (ADR-0007) together with the enforcement rules that exist *because*
   of it (ADR-0008 rules 1–5). The two policy follow-ups (all refusal reasons in the audit,
   and the exhaustive authority test) are a separate small spec.
2. **The run is owned by Temporal, and the record by Postgres.** This is the same split ADR-0006
   made for LangGraph: the orchestrator owns *where* a run is. `runs`, `waits`, `run_steps`,
   `approvals`, `idempotency_claims` and `audit` keep owning *what* happened.
3. **The LangGraph turn runs inside one activity.** The experimental
   `temporalio.contrib.langgraph` plugin is not used, and the Postgres checkpointer stays.
4. **Nothing is deployed, so no runs are in flight.** This is a cutover, not a drain. ADR-0002
   said the reference runtime must be swapped "before first deploy", and this is that swap.
5. **Dev hosting is `docker-compose.yml`.** Production hosting is deferred to ADR-0009, with its
   invariants fixed.
6. **`temporalio` becomes a runtime dependency.**
7. **The three runtime fitness tests are re-pointed, not rewritten.** They are
   `test_run_identity`, `test_idempotency` and `test_waiting_is_state`.

**Approving this spec is the "ask first"** for four things CLAUDE.md reserves:
- changing the durable-execution backend
- adding `temporalio` as a runtime dependency
- adding one `.importlinter` contract (below)
- adding the `temporal` service to `docker-compose.yml`

Nothing else it implies is pre-approved.

**Supersedes** two lines in `SPEC.md`: Tech Stack → Durable execution ("LangGraph… ADR-0002
resolves"), and Decided during review → Durable execution. On approval, both are amended to
point here, ADR-0007 moves to *accepted*, and ADR-0002 is closed against it. The current
text contradicts ADR-0002, which still says "deferred", and this resolves that.

---

## Objective

**The runtime a weeks-long experiment run actually needs, instead of the one we hand-maintain.**

T17–T21 built waits, re-ask deadlines, checkpoint shape, a strand guard and bounded fan-out
by hand, on Postgres. Temporal gives each of these as a primitive, verified against the SDK
rather than assumed: server-decided run identity (P1), waits and timers that survive process
death (P3, P4, P8), replay-checked deploys (P5) and bounded fan-out (P7).

What it does **not** give is the reason this spec is more than "add Temporal":

- **Activities are at-least-once (P2).** The gateway and its ledger stay, unchanged.
- **Each of ADR-0008's five rules (E1–E4) is a way the gateway's guarantees quietly stop
  holding** once a durable orchestrator sits in front of them. The most serious is E3: if
  the workflow carries the approval-time snapshot into the commit, **a stale approval commits.**

**Done means:** the product spec's run (trigger → evaluation → proposal → Slack approval →
rollout) runs as a Temporal workflow. It survives the same deaths it survives today, and
every guarantee in `STACK.md` rows 3, 7 and 8 still holds. It holds because a test asserts it
against the Temporal runtime, not because it held on the old one.

**Users:** the operator (runs `operator status/stalled`), the approver (unchanged Slack
flow), and the next engineer. For that engineer, "where is this run and what is it waiting
on" becomes answerable from one place.

---

## Tech Stack

| Concern | Choice | Notes |
|---|---|---|
| Orchestration | **Temporal**, `temporalio` ≥1.33 (Python SDK) | verified at 1.33.0 (ADR-0007) |
| Dev server | `temporalio/auto-setup` in `docker-compose.yml`, Postgres persistence, its **own database** | not `agentstack`: Temporal's schema is not ours to migrate |
| Prod server | **deferred**, see ADR-0009 | invariants fixed there |
| Turn execution | LangGraph, unchanged (ADR-0006) | one activity per turn |
| Record stores | Postgres, unchanged | `runs`, `waits`, `run_steps`, `approvals`, `idempotency_claims`, `audit` |
| Everything else | as `SPEC.md` | |

---

## Commands

Existing commands are unchanged. New or changed:

```
bash scripts/dev_up.sh                                  # now also starts temporal and waits for it
uv run agentstack-worker                                # NEW: the Temporal worker (workflows + activities)
uv run agentstack-operator status --experiment <id>     # now reads position from Temporal, record from Postgres
uv run agentstack-operator stalled --older-than 7d      # unchanged: still reads `waits`
uv run python scripts/replay_guard.py                   # NEW: replays recorded histories against this code
temporal workflow list --address localhost:7233         # inspect runs (dev)
```

`replay_guard.py` joins `checkpoint_guard.py` in `check_full.sh`. They guard different
things. The checkpoint guard covers the **turn graph's** state shape (T19/T20, still
LangGraph). The replay guard covers the **workflow's** determinism (P5).

---

## Project Structure

```
src/agentstack/runtime/temporal/        NEW package, layer 3
  contracts.py      dataclasses that cross workflow<->activity: ids and small verdicts only
  workflows.py      ExperimentWorkflow. Sandboxed. Imports temporalio + contracts, nothing else
  activities.py     evaluate_cycle, run_turn, park_wait, commit - each a thin shell over
                    existing runtime code; commit reads the snapshot itself (rule 3)
  retry.py          the one RetryPolicy; gateway refusals listed non-retryable (rule 2)
  interceptors.py   DeclaredActivitiesOnly (rule 5)
  worker.py         build_worker(): workflows, activities, interceptor, concurrency limits
  client.py         deliver_trigger(), notify_answer(): what layer 1 calls
src/agentstack/interfaces/worker_cli.py NEW  entry point for agentstack-worker
src/agentstack/runtime/deadlines.py     RETIRED once its tests pass against the timer loop
src/agentstack/runtime/fanout.py        RETIRED once its tests pass against worker limits
tests/fitness/test_temporal_boundaries.py   NEW
tests/durability/                       re-pointed at a real Temporal dev server
tests/fixtures/histories/               NEW  recorded workflow histories for the replay guard
scripts/replay_guard.py                 NEW
```

These stay, and keep their jobs: `cycles.py` (the trigger claim, the authoritative dedupe;
see Foundation Assumptions), `waits.py` (the wait *record*: what was asked, of whom, against
which snapshot), `steps.py`, `approvals.py` (layer 8 coordinator), `snapshot.py`, `graph.py`,
`nodes.py` and `loop.py`.

Retiring `deadlines.py` and `fanout.py` deletes code. It happens only in the task whose tests
prove the replacement, not before.

---

## Code Style

The existing code is the reference. The two things new here look like this:

```python
@workflow.defn
class ExperimentWorkflow:
    """Owns where the run is. Owns nothing else: no I/O, no clock, no authority."""

    @workflow.run
    async def run(self, start: RunStart) -> RunEnd:
        ...
        # Waiting is state. The wait row (what is asked, of whom) was written by
        # park_wait; this only decides when to look again.
        while not self.answered:
            try:
                await workflow.wait_condition(lambda: self.answered, timeout=REASK)
            except asyncio.TimeoutError:
                await workflow.execute_activity(reask, wait_id, **ACT)  # never silent expiry
        # No snapshot argument. The commit reads the world at the act (rule 3).
        return await workflow.execute_activity(commit, CommitIntent(run_id, proposal_id), **ACT)
```

```python
@activity.defn
def commit(intent: CommitIntent) -> CommitOutcome:
    snapshot = snapshots.current(intent)          # now, not when the approver looked
    envelope = envelopes.mint(intent.run_id)      # minted here, never carried in history
    return gateway.execute(request=..., envelope=envelope, state_snapshot=snapshot, ...)
```

Conventions:
- **Activity inputs are ids.** A `CommitIntent` names a proposal. It doesn't carry the
  proposal, the envelope or the snapshot.
- **Workflow code names activities through `contracts`,** never by importing
  `activities.py`. That import would drag `execution` and `storage` into the sandbox, and
  the sandbox doesn't object to it (E4).

---

## Testing Strategy

pytest. The existing levels, coverage floor (80% of changed lines) and ≥98% project ratchet
continue unchanged.

| Level | What it asserts here | Mechanism |
|---|---|---|
| Fitness | the ADR-0008 rules, as architecture | `test_temporal_boundaries.py`, plus the three re-pointed runtime tests |
| Durability | death and resume, re-pointed | a real dev server (compose), worker processes killed with SIGKILL, as in P4 |
| Elapsed time | re-ask, stalled deadlines, weeks of waiting | `WorkflowEnvironment.start_time_skipping()`: 72 simulated hours in <1s (P8) |
| Determinism | a deploy doesn't strand live runs | `Replayer` over `tests/fixtures/histories/` (P5), in `check_full.sh` |
| Infra | the substrate is up | `tests/infra` **fails** when Temporal is unreachable. It doesn't skip. |

**One risk, named now:** the workflow sandbox re-imports workflow modules, and whether
coverage.py sees code running inside it is **unverified**. T1 checks it first. If coverage
can't see sandboxed code, the fix is to measure it through an unsandboxed runner in the
coverage job. Excluding the module from coverage is not an option, and it would trip
`stack_guard` anyway.

---

## Layer Ownership Ledger

| # | Layer | Touched | What changes | Test that proves it |
|---|---|---|---|---|
| 1 | Interfaces | **yes, narrow** | Trigger ingress and the Slack callback hand off through `runtime.temporal.client` (signal-with-start, notify). They still resolve no identity and no policy. The Slack path still runs `ApprovalCoordinator` *before* notifying. | `test_layer_boundaries` (contract 4), `test_slack_inbound`, `test_trigger_asymmetry` |
| 2 | Control plane | **no** | Session ownership unchanged. The workflow id is `experiment-run:{run_id}`, which is neither a session id nor a principal. | `test_session_ownership` (unchanged, must stay green) |
| 3 | Runtime | **yes, heavily** | Temporal owns position, waits, timers, fan-out bound and continue-as-new. Postgres keeps the record. `deadlines.py` and `fanout.py` are retired. Gateway refusals are non-retryable, and `UnresolvedEffect` parks a reconcile wait. | re-pointed `test_run_identity`, `test_waiting_is_state`, `test_wait_gates_the_run`, `test_stalled_waits`; `tests/durability/*`; new `test_temporal_boundaries` |
| 4 | Model engine | **no** | Placement only: the model is called inside the `run_turn` activity, never from workflow code. Budgets unchanged. | `test_ollama_contract` (unchanged); sandbox refuses model I/O in workflow code (in `test_temporal_boundaries`) |
| 4b | Tabular inference | **no** | Scoring runs inside `evaluate_cycle`. The licence gate still fails at startup: the **worker** runs preflight before polling. | `test_prediction_gate` |
| 5 | Context / memory | **no** | Workflow state is **not** memory (see Boundary Decisions). The process-local `MemoryStore` gap (Checkpoint A) is untouched by this and stays open. | `test_memory_is_explicit` (unchanged) |
| 6 | Tools | **yes, a guard only** | No tool changes. A fitness test forbids `agentstack.tools` from importing `temporalio`, so a key can't be derived from Temporal identity (rule 1). | `test_idempotency` (+ retry/reset/redelivery land once, E1) |
| 7 | Execution | **no code change; relied on** | `gateway.execute` is unchanged. Its refusals now drive the retry policy (rule 2). | re-pointed `test_idempotency`, `test_unresolved_effects`, `test_capability_is_not_execution` |
| 8 | Identity / policy / approvals | **no code change; relied on** | Authority stays in `authorize_approver` and the `approvals` row. A Temporal Update validator (if used) checks shape and state only (rule 4). Envelopes are minted in activities and never enter history. | re-pointed `test_approver_authorisation`, `test_state_snapshot` (+ world moves after approval → `ApprovalStale`, E3) |
| 9 | Observability | **yes** | Spans carry our `run_id` across workflow and activities. **Temporal history is not audit**: `audit.records` stays the only accountability record. `operator status` joins Temporal's position with Postgres' record. | `test_trace_completeness`, `test_audit_separate_from_traces` |
| 10 | Infrastructure | **yes** | A `temporal` service in compose, with its own database. `dev_up.sh` waits for it. Prod hosting is deferred (ADR-0009). | `tests/infra` (Temporal unreachable fails the run) |

---

## Foundation Assumptions

| Concern | What this feature inherits | What it forces |
|---|---|---|
| **Delivery: activities** | at-least-once. A lost completion reruns an activity whose effect landed (P2). | every activity idempotent; effects only through the gateway ledger |
| **Delivery: workflow code** | re-executed on every replay; deterministic by contract | no I/O, clock or randomness except via `workflow.*`; enforced by the sandbox plus the import contract (the sandbox alone isn't enough, E4) |
| **Delivery: triggers** | at-least-once from the ingress, as today | dedupe on the Postgres cycle claim, **not** on workflow-id reuse |
| **Dedupe window** | workflow-id reuse policies only hold within namespace **retention** | a trigger redelivered after retention would start a fresh workflow. The Postgres claim is why that can't cause a second evaluation. |
| **Consistency** | Temporal history is strongly consistent per workflow. Postgres is read-committed. **No transaction spans both.** | the dual-write gap is real: an activity writes a row and dies before completing. Every activity write is an upsert or a claim, so a rerun converges. |
| **Isolation / failure** | worker death: the task waits out the sticky-queue timeout (default 10s) and then moves (P4). Server down: runs freeze and aren't lost. | resume-after-death budget ≤15s. Server outage is an availability event, not a correctness one. |
| **History size** | Temporal caps event history per workflow | continue-as-new every 100 cycles, carrying `run_id` and the current wait id. A weeks-long run never approaches the cap. |
| **Payloads** | history stores every activity input and output | ids and small verdicts only. **No envelope, credential, prompt or evidence bundle**, whether or not a codec is added later (ADR-0009). |
| **Model asset** | unchanged: `qwen3:8b` (see `SPEC.md`) | none |
| **Serving system** | unchanged: Ollama in dev, cloud GPU in prod, open | `run_turn`'s `start_to_close_timeout` must exceed turn p95 (60s): set to 120s, with a heartbeat |
| **Interaction contract** | unchanged: Ollama native tool calls | none |
| **Budgets** | p95 evaluation ≤60s (unchanged) | Temporal adds per-activity scheduling on the order of milliseconds. Resume after worker death ≤15s. |

---

## Boundary Decisions

| Question | Decision |
|---|---|
| **Session vs authorization.** What owns session identity, and how is it distinct from authorization? | Unchanged: the experiment run owns the session. The workflow is keyed by `run_id`. It holds **no authority**: no envelope, scope or credential is ever workflow state or an activity input. An envelope is minted inside the activity that uses it, from the session view, for that act. A weeks-long workflow accrues nothing. |
| **Transcript vs context.** What is the transcript store, and how is prompt context derived from it? | Temporal's event history is **neither**. The transcript stays in Postgres `transcript_events`. Context is assembled inside `run_turn` / `evaluate_cycle` from Postgres, per cycle. History records only that the activity ran and the ids it returned. |
| **Memory vs learning.** What is memory here, who scopes it, and when is it written? | Unchanged from `SPEC.md`: prior decisions on this experiment, written explicitly at cycle end. **Workflow state is not memory.** A value held in a workflow variable doesn't make it memory, and nothing is written to memory because the workflow happens to hold it. The `MemoryStore` durability gap stays open, and it's recorded as such rather than quietly "solved" by history. |
| **Capability vs execution.** Which capabilities are exposed, and which surface executes them? | Tools and exposure are unchanged. An **activity is an execution unit, not an authority**: only activities declared in `interceptors.py` run (rule 5), and the only one that commits (`commit`) does so through `gateway.execute`. Workflow code has no path to an effect: import contract plus sandbox. |
| **Approval vs isolation.** Where is the approval boundary, and what isolates the action afterwards? | The boundary is unchanged: immediately before the rollout call. Authority is the `approvals` row, written by `ApprovalCoordinator` after `authorize_approver`. The Slack path does this before notifying the workflow, and the workflow's wake-up carries no authority (rule 4). **"Immediately before" is enforced at the act:** `commit` reads the snapshot itself, so an approval against a world that has moved is `ApprovalStale` (rule 3, E3). Isolation after approval is unchanged: the sandbox, plus an envelope scoped to one act, minted in the activity. |
| **Observability vs evaluation.** What evidence is emitted, and what criteria judge it? | Temporal's history and UI are **orchestration debugging**. They are not the trace, not the audit and not an eval. Spans still carry our `run_id` end to end. `audit.records` stays the only accountability record, retained independently of Temporal's retention. Judgement is unchanged: the fitness suite and `evals/` gates, plus the replay guard for determinism. |
| **What identifies "this resource, now"?** | Unchanged: `(experiment_id, experiment_version, data_as_of)`. What's new is *where it's read*: in the `commit` activity at the act, never carried from the moment the approver was asked. |

---

## Boundaries

**Always**
- Route every side effect through `gateway.execute` from inside an activity, with an envelope
  minted in that activity and the tool builder's content-derived idempotency key.
- Read the state snapshot inside the `commit` activity, at the act.
- Use the one `RetryPolicy` from `runtime/temporal/retry.py`. Gateway refusals
  (`UnresolvedEffect`, `ApprovalStale`, `ApprovalRequired`, `PolicyDenied`,
  `SandboxViolation`, `ApproverNotAuthorized`) are non-retryable. `UnresolvedEffect` parks a
  reconcile wait.
- Keep workflow inputs, outputs and activity arguments to ids and small verdicts.
- Record a history fixture for every workflow change, and run the replay guard before merge.
- Dedupe triggers on the Postgres cycle claim.

**Ask first**
- Adding a dependency; changing a `.importlinter` contract; changing the durable-execution
  backend; widening a tool's scope (all standing).
- Adopting `temporalio.contrib.langgraph` or retiring the Postgres checkpointer.
- Choosing production hosting (ADR-0009).
- Changing namespace retention, the continue-as-new threshold, or worker concurrency limits.
- Deleting a history fixture, or using `workflow.deprecate_patch`.

**Never**
- Expose a tool that performs its own side effect.
- Write memory implicitly: not because the model said something, and not because a workflow
  variable holds something.
- Approve at task start for an act that happens later. Here that includes carrying an
  approval-time snapshot into the act.
- Let untrusted content reach an authority decision. That includes signal and update payloads,
  and a Temporal Update validator as the place authority is decided.
- Derive an idempotency key from `workflow_id`, `run_id`, `activity_id` or `attempt` (E1).
- Put an envelope, credential, prompt or evidence bundle into workflow history.
- Do I/O, read the clock or use randomness in workflow code, or import `execution`,
  `storage` or `activities` from it.
- Retry a gateway refusal, or retry `UnresolvedEffect` blind.
- Treat Temporal history as the audit record, or its retention as the dedupe window.
- Edit an applied migration. Fix forward (standing).

**The one import contract this adds** (approved with this spec):

```ini
[importlinter:contract:6]
name = Workflow code has no path to an effect (Parts 4 and 7)
type = forbidden
source_modules =
    agentstack.runtime.temporal.workflows
    agentstack.runtime.temporal.contracts
forbidden_modules =
    agentstack.execution
    agentstack.storage
    agentstack.policy
    agentstack.model
    agentstack.prediction
    agentstack.runtime.temporal.activities
```

---

## Success Criteria

Each becomes a test. Numbering continues from `SPEC.md` (1–28), which all still hold.

29. **One run per id, decided by the server.** Five concurrent starts for one `run_id`
    produce one workflow and one `runs` row. **(P1)**
30. **A redelivered trigger evaluates once, even after the workflow has closed.** It's
    deduped on the Postgres claim, with workflow-id reuse deliberately allowed in the test.
31. **Death while parked.** The worker is SIGKILLed while a run waits for approval. A fresh
    worker process resumes it through the real Slack path. `prepare`, `ask` and `commit` each
    happen once. Resume ≤15s. **(P4; re-points criterion 23)**
32. **Re-ask, never silent expiry, over simulated days.** An unanswered approval is asked again
    at every interval across 72 simulated hours, in under a second of wall time. **(P8)**
33. **A stale approval refuses at the act.** The world moves between approval and commit,
    and the result is `ApprovalStale`: no commit, audited, one attempt. **(E3; re-points 6)**
34. **An unresolved effect is never retried blind.** One attempt, then the run parks. Once
    reconciled, the re-run deduplicates and the surface is called once. **(E2; re-points 8)**
35. **Refusals aren't retried.** Each of the six gateway refusal types yields exactly one
    activity attempt.
36. **Keys survive Temporal.** Retry after commit, workflow reset and redelivery each land
    once. `agentstack.tools` doesn't import `temporalio`. **(E1)**
37. **Only declared activities run.** An undeclared activity is refused non-retryably and
    never reaches a surface. **(E4)**
38. **Workflow code can't reach an effect.** `lint-imports` enforces contract 6, and
    `stack_guard` flags its removal.
39. **An unauthorised approver changes nothing.** A notify from a user outside the approver
    group writes no approval, and the commit refuses. **(E3; re-points 25)**
40. **Nothing secret in history.** A fitness test decodes every payload in a recorded
    history and finds no envelope, credential ref, prompt or evidence field.
41. **A stranding deploy fails the build.** An unguarded change to `ExperimentWorkflow`
    fails `replay_guard.py` against the recorded histories, and the same change behind
    `workflow.patched()` passes. **(P5; alongside 22)**
42. **Bounded fan-out.** 100 triggers at once never run more evaluations concurrently than
    the configured worker limit. **(P7; re-points T21)**
43. **Stalled still surfaces.** `operator stalled` reports a trigger wait past its
    deadline, read from `waits` as today. **(re-points 4)**
44. **Continue-as-new is invisible to the record.** After continue-as-new, a run keeps its
    `run_id`, its current wait and its audit trail. Traces and `operator status` show one run.
45. **The substrate fails loudly.** With Temporal down, `tests/infra` fails, and a worker
    exits non-zero at start. It doesn't hang.

---

## Open Questions

1. **Does coverage see sandboxed workflow code?** Unverified, and checked first (see
   Testing Strategy). The answer changes how coverage is measured, never whether it is.
2. **Namespace retention in dev.** The proposal is 7 days, long enough to debug a failed run.
   Because of criterion 30, correctness doesn't depend on this value.
3. **Update or signal for the approval wake-up?** The Slack path authorises before it
   notifies, so the workflow only needs a wake-up. A signal is enough. An Update adds a
   synchronous "accepted" to the callback. This is a plan-time choice either way, since
   authority is not in it.
4. **Production hosting.** Deferred with its invariants in ADR-0009.
