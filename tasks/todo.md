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

- [x] **T4 — Durable approvals, idempotency ledger and audit sink** · layers 7, 8, 9 · *M*
  - Acceptance: approvals, the two-phase idempotency ledger and audit records in
    Postgres. `IN_FLIGHT` survives a restart — that is the state the whole design
    exists for. Audit retention and PII remain iteration-2 debts; durability does not.
  - Verify: `test_approval_boundary.py`, `test_unresolved_effects.py`,
    `test_audit_separate_from_traces.py` against Postgres.
  - Depends: T1. Files: ~4.
  - **Done.** `claim` is one atomic upsert (`ON CONFLICT DO UPDATE ... RETURNING
    xmax = 0`), not read-then-insert. Sabotage-verified: the old shape tells four
    concurrent callers they may all act.
  - `audit.records` sits in its own schema and carries **no** FK onto `runs`. Every
    other table cascades from sessions; an audit record must not, or a retention job
    erases accountability as a side effect of tidying up. Demonstrated by adding the
    cascading FK on a scratch database and watching the record vanish.
  - **T2's shared-database reasoning was wrong** and this is where it showed. An
    idempotency key is `refund:{tenant}:{charge}:{amount}`, stable across runs by
    design, so the first test to refund ch-7 settled it for every test after. Fixed by
    emptying the tables per test (`storage.provision.truncate_all`, guarded by the same
    disposable-name rule), not by weakening the key — cross-run double-refund protection
    is a real property worth keeping.

### ✅ Checkpoint A — the port weakened nothing
- [x] All **23** fitness tests green **against Postgres**, not fakes (22 at plan time;
      T1 added `test_the_bar_guards_itself.py`). No store is constructed empty in the
      suite except `MemoryStore` — see the gap below.
- [x] All 11 gates green, each against a migrated `agentstack_evals` database
- [x] `stack_guard.py --base main` clean; coverage 99%, changed-line 100%
- [x] `0001`–`0003` roll back to empty and forward again
- [ ] **Human review before Phase 2**

**Gap found at the checkpoint — not covered by any task in this plan.**

`agentstack.context.MemoryStore` and `MaintenanceQueue` are still in memory. `STACK.md`
layer 5 claims the authoritative store is a "retrieval index + `MemoryStore`", which is
true only in the sense that a Python dict is a store — memory does not survive the
process, which for a memory store is close to not existing.

T2–T4 covered layers 2, 3, 7, 8 and 9. Layer 5 was never assigned a durability task, and
T5's "cohort data loading" is a different layer-5 concern. So this is a planning gap, not
a skipped task.

It does not block Phase 2: nothing in the iteration-1 workflow writes a memory. It does
mean the durability claim is layer-shaped rather than whole, and it should be either
scheduled or explicitly deferred with a reason before Checkpoint F asserts the 28
criteria are met.

---

## Phase 2 — Real prediction

- [x] **T5 — Cohort data loading** · layer 5 · *S*
  - Acceptance: load `telecom-bigml` and `bank-churn` through `churn_tabpfn`'s existing
    registry and cleaning, with a stable snapshot watermark that becomes `data_as_of`.
  - Verify: new `tests/fitness/test_data_snapshot.py` — the same snapshot yields the
    same watermark and the same row count; the documented leakage columns stay dropped.
  - Depends: none. Files: ~3.
  - **Deviation:** `churn_tabpfn`'s source no longer exists. `~/Desktop/revenuecat` holds
    only `README.md`, `pyproject.toml` and a lockfile; the package is installed editable
    from a `src/` that is gone, and there is no git history there to recover it. The
    registry and cleaning were rebuilt in `agentstack/context/datasets.py` from the
    rules the README still documents. **If the source exists elsewhere, say so** — the
    cleaning here matches the documented behaviour but not necessarily the code.
  - `data_as_of` is a **content hash, not a timestamp**. A clock records when someone
    looked; two runs on identical data would disagree and criterion 17 ("same snapshot +
    same model version → same cohort") could never be checked.
  - The datasets are third-party and gitignored. `data/manifest.json` is committed, so a
    change upstream appears as a changed watermark in a diff.
  - `tests/live/` is the declared lane for real-data checks, excluded in one visible
    place in `pyproject.toml` with a `CONSTRAINTS.md` row — a directory, not a `skipif`.
  - Fetching lives in `scripts/`, outside the package: it reaches the network, and
    `lint-imports` contract 3 now names `kaggle` so that stays true.

- [x] **T6 — TabPFN classifier adapter and licence gate** · layer 4 · *M*
  - Acceptance: `prediction/` scores churn on pre-treatment features. A missing or
    invalid `TABPFN_TOKEN` is refused **at startup**. A committed fixture of recorded,
    seed-pinned scores for the dev snapshots lets CI test our adapter without the
    weights; one live test goes in a declared `live` suite.
  - Verify: new `tests/fitness/test_prediction_gate.py` — criterion 18, running in CI
    off the fixture. **No `skipif` anywhere.**
  - Depends: T5. Files: ~5.
  - **Done except the recorded fixture.** No `TABPFN_TOKEN` is configured on this
    machine, so `data/scores/` is empty and cannot be produced. Everything else is
    built and tested: the gate, the fold assignment, the encoding, the cross-fitting
    loop, the replay guards, and the startup command. `scripts/record_scores.py` makes
    the fixture in one command once a token exists; `tests/live` then checks it against
    the real model. **Action for a human: set `TABPFN_TOKEN`, run
    `uv sync --extra prediction`, then `uv run python scripts/record_scores.py`.**
  - **Closed the carried-forward gap:** `agentstack.prediction` was in none of the
    `.importlinter` contracts. It is now in all five. Sabotage-verified — an `httpx`
    import and an upward import into `execution` each break a contract that previously
    would not have noticed.
  - **Scores are out of fold.** TabPFN is zero-training but still conditions on the
    rows it is given, so in-sample scores would rank the rows the model fit best rather
    than the customers most at risk — and the targeted experiment would then measure
    regression to the mean, already named in `SPEC.md` as the easiest way to get a
    confident wrong answer. The fold assignment is ours, not a library's, because the
    same snapshot and seed must give the same cohort.
  - **`tabpfn` is an optional extra.** It pulls torch; a CI run checking the approval
    boundary should not build a deep-learning stack to do it. `uv sync --extra prediction`.
  - Ratchets: fitness tests 24 → 25; project coverage 99% → **98%**, recorded in
    `CONSTRAINTS.md` with the reason — three lines that call `TabPFNClassifier` cannot
    execute where the extra and the licence are absent.

- [x] **T7 — Targeting predicate and cohort freeze** · layers 4, 5 · *M*
  - Acceptance: top-decile risk cut, minimum cohort 1,000, value-at-risk floor $50k
    from observed ARPU. The predicate, the threshold and the model version are frozen
    into `experiment_version`.
  - Verify: new `tests/fitness/test_targeting.py` — criterion 17.
  - Depends: T6. Files: ~4.
  - **The spec's own defaults cannot be met by the dev data, and that is not a bug in
    either.** telecom-bigml has 3,333 customers, so its top decile is 334 against a
    1,000 minimum. The spec's rationale fails too: a 10% arm of 334 is 33 customers,
    not the ≥100 it wants. The value floor passes comfortably ($242k vs $50k).
  - Resolved with **named profiles** in `experiments/targeting.toml`: `default` is the
    specified production rule and still refuses here; `dev` relaxes the size floor,
    states what that costs, and puts its name into `experiment_version` and the cohort
    description so a dev cohort cannot be mistaken for a production one. A live test
    asserts `default` still refuses, so nobody "fixes" it by lowering the real number.
  - **bank-churn is loadable but not targetable.** It has no observed revenue —
    `Balance` is a deposit, `EstimatedSalary` is the customer's income, `Point Earned`
    is loyalty points. Turning a balance into revenue needs a net interest margin, and
    the floor is specified against *observed* ARPU. Refused with that reason rather
    than given a modelled substitute.
  - The cut is on **rank, not quantile**: a quantile threshold with ties returns more
    than a decile, and cohort size is exactly what the minimum-size gate is about.
  - Membership stays re-checkable (`Cohort.includes`) because criterion 11 needs every
    control subject shown to have passed the same predicate at the same model version.
  - **Still blocked on the fixture from T6:** the cohort above was exercised with
    stand-in scores. A real frozen cohort needs `TABPFN_TOKEN` and
    `scripts/record_scores.py`.

### ✅ Checkpoint B — a cohort is real and reproducible
- [ ] A cohort can be produced twice from one snapshot with identical membership
- [ ] Thresholds live in `experiments/`, versioned
- [ ] Human review

---

## Phase 3 — The durable runtime

- [x] **T8a — The turn as a LangGraph graph** · layer 3 · *M, verify first*
  - Acceptance: `run_turn`'s sequence becomes graph nodes with an in-process
    checkpointer. Run identity, step boundaries and wait semantics unchanged.
  - Verify: `test_run_identity.py` and `test_idempotency.py` pass through the graph.
  - Depends: T2, T3, T4. Files: ~4.
  - **Done. Verifying first paid for itself four times** — every one of these was an
    assumption that turned out wrong, and each would have been far more expensive to
    discover with Postgres underneath ([ADR-0006](../docs/adr/0006-langgraph-turn-execution.md)):
    1. `durability` defaults to `"async"`. Checkpoints are written without waiting, so
       a process that dies may have none. Every invocation now passes `"sync"`.
    2. Code before `interrupt()` runs **twice** on resume. Binds T17: a node that
       interrupts must do nothing before it interrupts.
    3. `checkpoint_ns` is the *subgraph* namespace. Using it to separate turns makes
       `get_state` raise "Subgraph not found". Both ids go in `thread_id`.
    4. `invoke(input, config)` restarts from the beginning whatever the checkpoint
       says; only `invoke(None, config)` resumes. Getting this wrong costs a second
       model call per recovery — exactly what the task exists to prevent.
  - **The state/context split is what makes T8b tractable.** State is plain data;
    the gateway, registry, engine and tracer travel in LangGraph's `context` and are
    never checkpointed. Putting a live database handle in state would have surfaced at
    T8b, at the most expensive moment to change it.
  - Resuming skips completed nodes, so anything a later node needs must be in state or
    recomputable. The model's answer is in state (it cannot be recomputed — asking again
    could differ); the bundle and the exposure filter are recomputed (they are
    deterministic). `act` keeps only the context *fingerprint*, which is all it ever used.
  - `interrupt()` is **not** adopted. Wait semantics are unchanged, per the acceptance;
    T17 decides it under constraint 2 above.

- [x] **T8b — Postgres checkpointer** · layer 3 · *M*
  - Acceptance: checkpoints round-trip through Postgres; a graph resumed from storage
    behaves identically to one that never stopped.
  - Verify: `test_wait_gates_the_run.py` through the graph, resumed from storage.
  - Depends: T8a. Files: ~3.
  - **Done.** `PostgresSaver` has no schema parameter, so `migrations/0004` creates a
    `langgraph` schema and the saver gets a pool pinned to it. Otherwise its four tables
    and its own ledger (`checkpoint_migrations`) sit in `public` beside ours, and
    `migrate down --to 0` leaves four tables nobody can account for.
  - Three pool settings that are requirements, not preferences: **autocommit** (`setup()`
    uses `CREATE INDEX CONCURRENTLY`, refused in a transaction — and a write inside an
    open transaction when the process dies is not a write), **`row_factory=dict_row`**
    (the saver reads mappings; a tuple-row pool type-checks and fails at first read),
    **`prepare_threshold=0`**.
  - The checkpointer is the one piece of infrastructure that comes *after* migrations.
    Postgres says "no schema has been selected to create in"; `open_checkpointer` now
    names the schema and the command instead.
  - `build_stack` requires a checkpointer, like `db`. The only sensible default is the
    in-memory one, and a durable-looking runtime backed by a dictionary is exactly what
    this phase exists to remove.
  - **My first version of the resume test was wrong**: both stacks shared one saver
    object, so it passed with an in-memory checkpointer too — it proved the object was
    shared, not that anything was stored. The replacement stack now opens its own pool
    and saver, and sabotage confirms the assertion fails without shared storage.

- [x] **T9 — Trigger ingress** · layers 1, 3 · *M*
  - Acceptance: an endpoint accepting a trigger with `kind` and `data_as_of`; the
    evaluation is idempotent on `(experiment_id, data_as_of)`; `metric_movement` cannot
    reach a propose.
  - Verify: criterion 3 gate plus the look-vs-decide asymmetry.
  - Depends: T8b. Files: ~3.
  - **Done.** Two new gate cases (13 total): a trigger delivered three times evaluates
    once, and a `metric_movement` reaching a propose is refused.
  - **Deviation from the spec's wording, for review.** The spec says the cycle is
    "keyed on `data_as_of`"; the key is `(experiment_id, data_as_of, kind)`. The hazard
    is a broker redelivering *the same message*, which carries the same kind, so this
    covers it. Dropping `kind` would also collapse a `data_arrival` into an earlier
    `metric_movement` at the same watermark — the trigger that may propose silently
    suppressed by the one that may not. That is an authority downgrade arriving
    disguised as a deduplication.
  - The asymmetry is held **twice**: `policy.triggers.authorize` refuses at the point
    of recording (the evaluator is the thing that might be wrong), and a CHECK
    constraint refuses a `metric_movement` row carrying a propose.
  - A refused outcome leaves the cycle **claimed and unsettled**, visible via
    `unsettled()` — the same shape as an unresolved idempotency claim. Recording some
    safer outcome instead would be inventing a decision to tidy up a refusal.
  - **Contract 4 caught a design error**: `runtime` cannot import the channel layer, so
    `TriggerEvent` moved to `policy/triggers.py` beside the authority table. The parsing
    of an untrusted payload stayed in layer 1, which is its job.
  - The plan said "the existing look-vs-decide asymmetry test" — there wasn't one. It
    exists now (`tests/fitness/test_trigger_asymmetry.py`, 20 tests).

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
