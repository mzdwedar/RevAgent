# Implementation Plan: Experiment Operator, iteration 1

Derived from `SPEC.md` (approved, revision 10). Tasks are in `tasks/todo.md`.

## Overview

Build the first half of the experiment lifecycle — **score → target → propose →
launch** — end to end, on durable infrastructure, with a real churn model and a real
human in Slack. The second half (measure → promote) is iteration 2.

The nine layers, the gateway, the approval machinery, the 22 fitness tests and the 11
release gates already exist and are green. This plan does not rebuild them. It makes
them durable, gives them a real workload, and adds the two ingresses they have never
had.

## Architecture decisions carried in from the spec

- **LangGraph** for durable execution, Postgres checkpointer. The existing
  `Wait`/`ResumeEvent`/`StepLedger` semantics are the contract; LangGraph is how they
  survive a process.
- **TabPFN classifier is real from task 6.** Zero-training, so there is nothing to stub
  around. The regressor and the covariate are iteration 2.
- **The model drafts the candidate**, and that output travels the full chain —
  exposure filter, schema validation, policy, gateway — before anything is written.
- **Slack is an untrusted ingress.** Signature verification is in scope even though
  credential *storage* is not.
- **Fail loud and park** on checkpoint incompatibility; `needs_migration` is a distinct
  state from `stalled`.

## Two decisions this plan makes that the spec left implicit

1. **The rollout surface is a local fake in iteration 1.** No external rollout API
   exists yet and its credential is deferred. Building against a fake HTTP service that
   can be made to time out is what exercises `UnresolvedEffect` against a real socket
   rather than a Python exception — which is the whole point of that code. The
   `SurfaceClient` protocol means swapping it is contained. **Flagged for review.**
2. **Audit records become durable in task 4, retention does not.** Accountability
   records that vanish on restart are not accountability records. The *retention and
   PII* policy remains the named iteration-2 debt; durability is not optional.

## Dependency graph

```
      T1 Postgres + migrations
              │
    ┌─────────┼──────────┬────────────────┐
    │         │          │                │
   T2        T3         T4              T5 datasets
 control   steps +   approvals +          │
  plane     waits    idempotency         T6 TabPFN + licence gate
    │         │          │                │
    └─────────┴────┬─────┘               T7 targeting + cohort freeze
                   │                      │
         T8a graph  →  T8b PG checkpointer ┘
                   │
              T9 trigger ingress
                   │
              T10 kill / resume
                   │
      ┌────────────┼─────────────┐
   T11 PRE_COMMIT  T12 Ollama    │
      └────────────┴──► T13 draft tool + registry surface
                              │
                        T14 Slack out
                              │
                   T15 Slack in (signature)
                              │
                   T16 approver group (policy)
                              │
                   T17 approve → resume → roll out
                              │
       ┌──────────┬───────────┼───────────┐
      T18        T19         T20         T21
    deadlines  migration   CI guard   concurrency
```

Slices are vertical after T8: each of T9–T17 adds one visible step of the end-to-end
path and leaves the system working.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| LangGraph API differs from memory | High — T8a blocks everything after it | `source-driven-development` at T8a, and T8a runs on an in-process checkpointer so the API is proven before Postgres is underneath it. |
| Porting to Postgres silently weakens a fitness test | **High** — the bar is the project's whole value | Checkpoint A requires all 22 green *against Postgres*, not against fakes. No task after T4 starts until it is. |
| TabPFN weights are licence-gated; CI has no token | Medium | T6 makes it a startup check. Prediction tests skip explicitly with a named reason when `TABPFN_TOKEN` is absent; the skip is visible, never silent. |
| Local model drafts malformed candidates | Low | Already handled: `InvalidToolArguments` → `tool.reject` → the turn still answers. T12 proves it with a real model rather than a fixture. |
| Concurrency target unreachable on a laptop | Low | T21 verifies at 100 and records the number actually achieved. A documented lower number beats an aspirational higher one. |
| The fake rollout surface diverges from the real API | Medium | Contain it behind `SurfaceClient`; T17's tests assert on gateway behaviour, not on the fake's internals. |

## Definition of Done

Every task clears `references/definition-of-done.md` plus `CONSTRAINTS.md`: fast checks
green, changed-line coverage ≥80%, project coverage ratchet held at 98%, all fitness
tests and all gates green, and `stack_guard.py` clean. A task is not done because its
own test passes.

## Answered at review

1. **Fake rollout surface: approved** for iteration 1.
2. **No `TABPFN_TOKEN` in CI.** See below — this does not mean skipped tests.
3. **Postgres in dev: docker-compose**, brought up by T1.

### The no-token consequence, worked through

The obvious implementation is `@pytest.mark.skipif(not os.getenv("TABPFN_TOKEN"))`.
That trips `stack_guard`'s own skip detector on every commit, and the tempting fix
would be to loosen the guard — which is exactly the move it exists to catch. So the
plan does not skip.

**Split by what is actually being tested.** TabPFN's weights are not our code; the
adapter, the targeting predicate and the cohort selection are.

| | Runs in CI | How |
|---|---|---|
| Adapter contract, targeting, cohort reproducibility | **yes** | against a committed fixture of recorded scores for the dev snapshots, seed-pinned as the benchmark already is |
| "the real model still returns scores in the expected shape" | no | one test in a declared `live` suite, run locally and nightly |

The `live` suite is declared as a row in `CONSTRAINTS.md` with its own `Runs at` value,
not hidden in a decorator. A lane where some tests do not run on PRs is acceptable when
it is written where people look; it is not acceptable as a quiet marker.

The honest limit: CI proves our code handles the model's output correctly. It does not
prove the model still works. That is what nightly is for, and it is stated rather than
implied.

---

## Phase 7 — Experiment registry and narrow registry tools

Spec: `SPEC-registry.md` (approved 2026-09-24; open questions taken at their defaults,
Q2 refined below). Tasks: `tasks/todo.md` § Phase 7.

```
T22 stage split ──┐
T23 schema ───────┼──► T25 Postgres client ──► [G] ──► T26 reads ─┐
T24 refusal ──────┘                                    T27 draft  ├──► [H] ──► T30 ──► T31
                                                       T28 abstain│
                                                       T29 halt  ─┘
```

T22–T24 are independent; T26–T29 are independent once T25 lands.

**Design decisions from reading the code**

- **One statement per write.** `Database` has no transaction scope, deliberately. Each
  transition is `WITH moved AS (UPDATE experiments … WHERE status = … RETURNING …)
  INSERT INTO registry_events SELECT … FROM moved`. Zero rows = precondition miss,
  provably nothing applied.
- **Refused is not unresolved.** A precondition miss raises `SurfaceRefused`; the
  gateway calls `IdempotencyLedger.abandon` — the case its docstring already names — and
  audits `refused`. This refines spec Q2 ("finalize as refused"): `abandon` is the
  existing mechanism, and a refused key must stay free for a later legitimate call.
- **Draft resource path unchanged**, so existing fingerprints and approvals stay valid.
  All new resources sit under `{tenant}/experiments/`, the existing sandbox prefix; the
  list read uses the trailing-slash path, so containment is not widened.
- **Idempotency keys are business identity**: revise and abstention hash their text;
  discard and halt are one per version.

**Risks**

| Risk | Mitigation |
|---|---|
| Stage rename trips `checkpoint_guard` | handle the version bump in T22, never loosen the guard |
| `stack_guard` reads a new tool as an approval downgrade | every tool lands at its final tier in its first commit |
| Fake and Postgres clients drift | one contract suite runs against both (T25) |

## Phase 8 — Durable runtime on Temporal

Spec: `SPEC-durable-runtime.md` (approved 2026-09-24). Evidence: ADR-0007, ADR-0008.
Tasks: `tasks/todo.md` § Phase 8. **Starts after Phase 7 is complete.** T24's
`SurfaceRefused` and T25's Postgres registry client are what the turn activity will
be committing through, and rewriting the runtime under a phase that is still
changing it is the collision T22 already warned about.

**Amended 2026-09-27 (human decision): Phase 8 goes ahead of T26–T31.** T24 and T25,
the stated reason for the dependency, have landed. The one known collision is
`tests/durability/test_concurrency.py`: T30 adds 20 concurrent halts to it, and T45
re-points it at the worker. Whichever of the two lands second rebases onto the other.

```
T32 substrate ─► T33 skeleton ─► T34 worker ─► T35 retry+interceptor ─► [I]
[I] ─► T36 trigger loop ─► T37 ingress ─► T38 trigger waits ─► [J]
[J] ─► T39 keys guard ─► T40 turn activity ─► [K]
[K] ─► T41 approval wait ─► T42 answer notify ─► T43 commit activity ─► T44 death e2e ─► [L]
[L] ─► T45 fan-out ─┐
       T46 cont-as-new ├─► T48 history audit ─► T49 traces ─► T50 status ─► T51 ledger ─► [M]
       T47 replay guard┘
```

T45–T47 are independent once [L] passes. Everything before [L] is one path, in order.

**Why this order**

- **Effects arrive last, one boundary at a time.** [I] has no activity that can touch a
  surface. [J] runs a real trigger through a real cycle with still no effect. The first
  gateway call from inside an activity is T40, and the first irreversible one is T43.
  Each sits behind its own checkpoint, because those are the places a wrong turn is
  expensive to undo.
- **The retry policy and the interceptor (T35) land before any activity can commit.**
  Rule 2 (refusals are never retried) and rule 5 (only declared activities run) are
  cheaper to have from the start than to retrofit onto code that already works.
- **Record stays, orchestration moves.** No task changes `runs`, `waits`, `run_steps`,
  `approvals`, `idempotency_claims` or `audit`. The existing fitness tests on those
  keep asserting the record. The durability tests move to asserting the
  orchestration, against a real worker process.
- **Retirements happen in the task that proves the replacement.** `deadlines.py` goes in
  T41 and `fanout.py` in T45, each only once its old tests pass against the new
  mechanism.

**Design decisions from reading the code**

- **"Re-point" means an extra driver, not a rewrite.** `test_run_identity`,
  `test_waiting_is_state` and `test_idempotency` assert properties of the Postgres
  record, which is unchanged, so they stay as they are and must stay green. What moves
  is `tests/durability/*`: `worker.py` and `park_worker.py` become Temporal worker
  processes, killed with SIGKILL as in probe P4.
- **The Slack path authorises before it notifies** (spec Open Question 3, answered):
  `ApprovalCoordinator.apply` runs in the callback process as today, then
  `client.notify_answer` sends a **signal**. A signal is enough because the workflow
  only needs waking. An Update would add a synchronous reply and no authority.
- **Activities wrap existing functions and add no logic.** `evaluate_cycle` calls
  `cycles.evaluate`, `run_turn` calls the graph, and `commit` calls `gateway.execute`.
  If an activity grows a decision of its own, that decision is in the wrong layer.
- **The dev namespace retention is 7 days** (spec Open Question 2). Nothing depends on it,
  because criterion 30 dedupes on the Postgres claim.

**Risks**

| Risk | Impact | Mitigation |
|---|---|---|
| coverage.py cannot see sandboxed workflow code | High: the 98% ratchet would drop or be gamed | **T32 checks this first.** The fix is to measure through an unsandboxed runner in the coverage job, never to exclude the module |
| Two suites sharing one Temporal namespace collide, as Postgres did at T22 | Med: spurious failures | per-invocation task-queue names from the start (T32 fixture) |
| `start_time_skipping()` downloads a Java test server at runtime | Med: CI without network fails | pin and cache the binary in CI (T38, where it is first used); fail, don't skip, when absent |
| Update validators tempt authority checks into the sandbox | High: rule 4 broken quietly | signals only (decision above). `test_temporal_boundaries` asserts no `@workflow.update` carries a validator that imports `policy` |
| A dual write (row written, activity dies before completing) | Med: duplicate rows | every activity write is an upsert or a claim; T36 and T41 each test a rerun of their activity |
| Replay guard fixtures go stale | Med: false confidence | T47 records fixtures from the durability tests themselves, and deleting one is ask-first (spec Boundaries) |
