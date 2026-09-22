# Tasks: Experiment Operator, iteration 1

Plan: `tasks/plan.md`. Spec: `SPEC.md`. Every task names the layer it touches and the
test that proves it, per `.claude/commands/plan.md`.

Standing bar on every task (`CONSTRAINTS.md`): `check_task.sh` green, changed-line
coverage ≥80%, project coverage ≥98%, all fitness tests, all gates, `stack_guard`
clean.

---

## Phase 1 — Durable foundation

Everything in iteration 1 rests on state surviving a process. Nothing here is new
behaviour; it is the same behaviour that stops being a lie when the process dies.

- [x] **T1 — Postgres, migrations, pooling** · layer 10 · *S*
  - Acceptance: `docker-compose.yml` for Postgres, a migration runner, a versioned
    schema directory, a pooled connection factory, and dev bring-up in one command.
  - Verify: `uv run pytest tests/infra/test_migrations.py` — migrate up from empty,
    down, and up again on a fresh database.
  - Depends: none. Files: ~4.
  - **Done.** New bottom layer `agentstack.storage` holds the driver — not
    `execution`, because the agent's own state is substrate, not a surface it acts
    upon ([ADR-0005](../docs/adr/0005-state-substrate-and-migrations.md)). That
    amended a floor rule in `CONSTRAINTS.md`, which `stack_guard` could not see, so
    T1 also closed that blind spot and added the *Amendments to the floor* log as the
    way a deliberate change is acknowledged. `lint-imports` contract 5 and a narrowed
    per-layer client exemption both enforce the driver rule.
  - Consequence: **Postgres must be up to run the suite** (`bash scripts/dev_up.sh`).
    `tests/infra` fails loudly instead of skipping; CI gets a service container.

- [x] **T2 — Durable control plane** · layer 2 · *M*
  - Acceptance: sessions, transcripts and working state in Postgres behind the existing
    interfaces. `session_id` is still never the user id; the runtime still receives a
    bounded `SessionView`.
  - Verify: `tests/fitness/test_session_ownership.py` passes **against Postgres**.
  - Depends: T1. Files: ~4.
  - **Done.** `migrations/0001` adds three tables, and `session_id <> user_id` is now a
    CHECK constraint as well as a constructor guard. The implicit `_sessions` dict in
    `SessionResolver` became a named `SessionStore`, so layer 2 has three stores rather
    than two and a dictionary.
  - The seam is `storage.database.Database` — SQL in, tuples out — so each store keeps
    its own SQL next to the invariants that SQL holds, instead of the stores migrating
    into layer 10 to satisfy contract 5.
  - **No in-memory variant.** A fake here would be a second implementation of the only
    thing these tests exist to prove, and it would be the one the suite exercised.
  - `build_stack` now requires a `Database`. Three test substrates, kept apart:
    `agentstack_test` (destructive, migrator only), `agentstack_app_test` (shared,
    migrated once), `agentstack_evals` (gates). `rebuild_database` refuses any name that
    does not end in `_test` or `_evals`.

- [x] **T3 — Durable step ledger and wait store** · layer 3 · *M*
  - Acceptance: steps and waits in Postgres. A completed step is still skipped on
    replay; an unsatisfied wait still blocks the run.
  - Verify: `test_waiting_is_state.py`, `test_wait_gates_the_run.py`,
    `test_step_identity.py` against Postgres.
  - Depends: T1. Files: ~4.
  - **Done**, and `migrations/0002` includes a `runs` table beyond the stated scope: a
    step or wait keyed on a run id nothing records is an orphan, with no row saying
    which tenant it belongs to and nothing for T10 to resume from. Run identity is the
    Part 4 invariant these two hang off.
  - Two invariants moved from code into the schema: `run_steps_complete_once` (partial
    unique index) refuses a second completion, and `satisfied_waits_record_when` refuses
    a wait marked satisfied with no time.
  - `_satisfy` now updates `WHERE ... AND NOT satisfied`. The read-then-decide checks in
    `resume` cannot settle two approvals landing together; without this both callers
    were told they resumed the run. Sabotage-verified.
  - `storage.database.IntegrityViolation` translates driver constraint errors, since
    layers above cannot import psycopg to catch them (contract 5). It carries the
    constraint name, so callers distinguish causes without parsing error strings.
  - A `started` row with no outcome is left as-is and reported by
    `started_but_unfinished`. Recording it as `failed` would invite a clean retry of an
    effect that may already have landed — T4's ledger is what settles it.

- [ ] **T4 — Durable approvals, idempotency ledger and audit sink** · layers 7, 8, 9 · *M*
  - Acceptance: approvals, the two-phase idempotency ledger and audit records in
    Postgres. `IN_FLIGHT` survives a restart — that is the state the whole design
    exists for. Audit retention and PII remain iteration-2 debts; durability does not.
  - Verify: `test_approval_boundary.py`, `test_unresolved_effects.py`,
    `test_audit_separate_from_traces.py` against Postgres.
  - Depends: T1. Files: ~4.

### ✅ Checkpoint A — the port weakened nothing
- [ ] All 22 fitness tests green **against Postgres**, not fakes
- [ ] All 11 gates green
- [ ] `stack_guard.py --base main` clean
- [ ] Human review before Phase 2

---

## Phase 2 — Real prediction

- [ ] **T5 — Cohort data loading** · layer 5 · *S*
  - Acceptance: load `telecom-bigml` and `bank-churn` through `churn_tabpfn`'s existing
    registry and cleaning, with a stable snapshot watermark that becomes `data_as_of`.
  - Verify: new `tests/fitness/test_data_snapshot.py` — the same snapshot yields the
    same watermark and the same row count; the documented leakage columns stay dropped.
  - Depends: none. Files: ~3.

- [ ] **T6 — TabPFN classifier adapter and licence gate** · layer 4 · *M*
  - Acceptance: `prediction/` scores churn on pre-treatment features. A missing or
    invalid `TABPFN_TOKEN` is refused **at startup**. A committed fixture of recorded,
    seed-pinned scores for the dev snapshots lets CI test our adapter without the
    weights; one live test goes in a declared `live` suite.
  - Verify: new `tests/fitness/test_prediction_gate.py` — criterion 18, running in CI
    off the fixture. `CONSTRAINTS.md` gains a row declaring the `live` suite and where
    it runs. **No `skipif` anywhere** — a conditional skip trips our own floor, and the
    fix for that is not to loosen the floor.
  - Depends: T5. Files: ~5.

- [ ] **T7 — Targeting predicate and cohort freeze** · layers 4, 5 · *M*
  - Acceptance: top-decile risk cut, minimum cohort 1,000, value-at-risk floor $50k
    from observed ARPU. The predicate, the threshold and the model version are frozen
    into `experiment_version`.
  - Verify: new `tests/fitness/test_targeting.py` — criterion 17: same snapshot + same
    model version → same cohort, size and risk distribution recorded.
  - Depends: T6. Files: ~4.

### ✅ Checkpoint B — a cohort is real and reproducible
- [ ] A cohort can be produced twice from one snapshot with identical membership
- [ ] Thresholds live in `experiments/`, versioned
- [ ] Human review

---

## Phase 3 — The durable runtime

- [ ] **T8a — The turn as a LangGraph graph** · layer 3 · *M, verify first*
  - Acceptance: `run_turn`'s sequence becomes graph nodes with an in-process
    checkpointer. Run identity, step boundaries and wait semantics unchanged.
  - **Before writing code:** `source-driven-development` against current LangGraph
    docs. The API is assumed here, not known — and finding that out with an in-process
    checkpointer is much cheaper than finding it out with Postgres underneath.
  - Verify: `test_run_identity.py` and `test_idempotency.py` pass through the graph.
  - Depends: T2, T3, T4. Files: ~4.

- [ ] **T8b — Postgres checkpointer** · layer 3 · *M*
  - Acceptance: checkpoints round-trip through Postgres; a graph resumed from storage
    behaves identically to one that never stopped.
  - Verify: `test_wait_gates_the_run.py` through the graph, resumed from storage rather
    than from memory.
  - Depends: T8a. Files: ~3.

- [ ] **T9 — Trigger ingress** · layers 1, 3 · *M*
  - Acceptance: an endpoint accepting a trigger with `kind` and `data_as_of`; the
    evaluation is idempotent on `(experiment_id, data_as_of)`; `metric_movement` cannot
    reach a propose.
  - Verify: criterion 3 gate plus `test_wait_gates_the_run.py`; the existing
    look-vs-decide asymmetry test now runs against the real ingress.
  - Depends: T8b. Files: ~3.

- [ ] **T10 — Kill and resume in a fresh process** · layer 3 · *M*
  - Acceptance: a parked run resumes from Postgres in a process that did not write the
    checkpoint.
  - Verify: new `tests/durability/test_process_death.py` — criterion 1. Spawns a real
    subprocess, kills it, rebuilds. A same-process resume does not count.
  - Depends: T8b. Files: ~2.

### ✅ Checkpoint C — the runtime is genuinely durable
- [ ] A triggered run scores a real cohort, is killed mid-flight, and resumes
- [ ] Checkpoint round-trips through Postgres, not memory
- [ ] Human review before side effects are wired

---

## Phase 4 — The model drafts, the registry records

- [ ] **T11 — Give `PRE_COMMIT` real semantics** · layer 8 · *S*
  - Acceptance: `PRE_COMMIT` grants a policy approval — audited with the rule that
    granted it, no human woken. `ALWAYS` still requires a human. Closes the
    three-tiers-two-behaviours defect the spec surfaced.
  - Verify: new `tests/fitness/test_approval_tiers.py` — criterion 20.
  - Depends: none. Files: ~3.

- [ ] **T12 — Ollama model engine adapter** · layer 4 · *M*
  - Acceptance: a `ModelEngine` implementation over a local Ollama model, emitting tool
    proposals. `num_ctx` explicit and recorded in Foundation Assumptions.
  - Verify: new `tests/fitness/test_ollama_contract.py` — a malformed proposal still
    produces `tool.reject` and an answered turn, now from a real model.
  - Depends: none. Files: ~3.

- [ ] **T13 — Candidate drafting tool and registry surface** · layers 6, 7 · *M*
  - Acceptance: replace the placeholder catalog with `create_experiment_draft`
    (side-effecting, reversible, `PRE_COMMIT`) and `roll_out_variant_to_percentage`
    (irreversible, `ALWAYS`, carrying the cohort predicate). Registry client lives in
    `execution/surfaces.py` and nowhere else.
  - Verify: criterion 19 — a proposal with a missing field, wrong type, extra argument
    or unexposed tool is refused before the registry is touched.
  - Depends: T11, T12, T7. Files: ~5.

### ✅ Checkpoint D — untrusted model output reaches a real side effect safely
- [ ] A model-drafted candidate is validated, policy-checked and written
- [ ] A malformed one is refused, traced, and answered
- [ ] The write produces a `PRE_COMMIT` audit record naming the rule
- [ ] Human review

---

## Phase 5 — Approval through Slack

- [ ] **T14 — Slack outbound** · layer 1 · *M*
  - Acceptance: post the rendered `ApprovalPrompt` as an interactive message with the
    headcount and dollar figure.
  - Verify: criterion 15 — the message names estimated customers affected, not a bare
    percentage.
  - Depends: T13. Files: ~3.

- [ ] **T15 — Slack inbound and transport authentication** · layer 1 · *M*
  - Acceptance: signature over the raw body, timestamp freshness, replay rejection. The
    adapter passes the user id onward as a **claim** and decides nothing with it.
  - Verify: criterion 24 — bad signature, stale timestamp and replayed body each
    refused before any policy runs.
  - Depends: T14. Files: ~3.

- [ ] **T16 — Approver authorisation** · layer 8 · *S*
  - Acceptance: approver-group membership checked in policy, scoped to the acting
    tenant. Tenant A's rollout is not approvable by tenant B's people.
  - Verify: criterion 25 — a correctly-signed interaction from an outside user is
    refused in layer 8, not in the adapter.
  - Depends: T15. Files: ~3.

- [ ] **T17 — Approve → resume → roll out** · layers 3, 7 · *M*
  - Acceptance: a Slack approval grants an `ApprovalRecord` bound to
    `(run_id, fingerprint, experiment_version, data_as_of)`, satisfies the wait, and the
    rollout commits exactly once through the gateway against the fake surface.
  - Verify: criterion 23 — the full path, with the process killed while parked and
    restarted before the human answers.
  - Depends: T16, T10. Files: ~4.

### ✅ Checkpoint E — the end-to-end path runs
- [ ] Trigger → score → target → draft → registry → Slack → approve → rollout
- [ ] Killed while parked, resumed, still correct
- [ ] Stale approval refused; unresolved effect not retried blind
- [ ] Human review

---

## Phase 6 — Production hardening

- [ ] **T18 — Wait deadlines and stalled detection** · layer 3 · *M*
  - Acceptance: every `trigger` wait carries a deadline; `operator stalled
    --older-than` reports runs past it; the approval re-ask timer fires.
  - Verify: criterion 4.
  - Depends: T17. Files: ~3.

- [ ] **T19 — Checkpoint versioning and `needs_migration`** · layer 3 · *M*
  - Acceptance: every checkpoint carries a schema version; an incompatible one parks
    the run in `needs_migration`, distinct from `stalled`, and never resumes into a
    shape it does not understand.
  - Verify: criterion 21.
  - Depends: T8b. Files: ~3.

- [ ] **T20 — CI guard against a stranding deploy** · layer 10 · *S*
  - Acceptance: CI compares the checkpoint schema against the last release and fails an
    incompatible change that ships no migration note.
  - Verify: criterion 22 — a deliberate incompatible change fails the build.
  - Depends: T19. Files: ~2.

- [ ] **T21 — Concurrency** · layers 2, 3, 10 · *M*
  - Acceptance: trigger fan-out without stampede, pooling sized for the target, 100
    concurrent runs verified. Record the number actually achieved.
  - Verify: new `tests/durability/test_concurrency.py`.
  - Depends: T17. Files: ~4.

### ✅ Checkpoint F — iteration 1 complete
- [ ] All 28 success criteria in `SPEC.md` met or explicitly deferred with a reason
- [ ] The `live` suite has been run at least once against the real model
- [ ] Fitness tests and gates green; ratchets held
- [ ] `/stack-audit` run and its findings addressed
- [ ] The two named debts still named: PII in traces, secrets in environment variables
