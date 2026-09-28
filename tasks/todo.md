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

- [x] **T10 — Kill and resume in a fresh process** · layer 3 · *M*
  - Acceptance: a parked run resumes from Postgres in a process that did not write the
    checkpoint.
  - Verify: new `tests/durability/test_process_death.py` — criterion 1.
  - Depends: T8b. Files: ~2.
  - **Done, with a real kill.** `tests/durability/worker.py` is started with
    `python -m`, runs a turn, hangs inside `act` on a surface that never answers, and
    is killed with **SIGKILL** — no atexit, no flush, no pool close. The test asserts
    the exit code is `-9` first, because if the worker exited cleanly the rest of the
    file proves nothing.
  - The kill is timed off a marker the hanging surface writes, so the model call is
    already checkpointed when the signal lands. Killing earlier would prove nothing
    about resuming past it.
  - **Model calls are counted across both processes** by appending PIDs to a file. The
    resumed turn must add no line. Under sabotage the failure reads
    `['42958', '42951']` — two different processes, which is the evidence this test
    exists to produce.
  - Two sabotages confirm it depends on durable storage: disabling resume, and swapping
    the Postgres saver for an in-memory one, both fail it.
  - A companion test asserts the resumed turn **did** reach the surface — resuming
    without re-calling the model must not mean resuming without doing the work.
  - Runs in the default suite; `check_task.sh` is 12s against a 90s budget.

### ✅ Checkpoint C — the runtime is genuinely durable
- [x] **Checkpoint round-trips through Postgres, not memory.** Four tables in the
      `langgraph` schema; a SIGKILLed worker's turn resumes in another process without
      re-calling the model, and both sabotages (resume disabled, in-memory saver) fail it.
- [ ] **A triggered run scores a real cohort, is killed mid-flight, and resumes** —
      **partially met, and the shortfall is a planning gap, not a skipped task.**
- [ ] **Human review before side effects are wired**

**What is actually true.** Every piece exists and is tested on its own:

| | |
|---|---|
| Trigger ingress, idempotent cycle, authority asymmetry | T9 ✓ |
| Cohort loads, watermarked, reproducible | T5 ✓ |
| Targeting predicate, frozen `experiment_version` | T7 ✓ (on stand-in scores) |
| Killed mid-flight, resumes in a fresh process | T10 ✓ |

**Two gaps between those and the checkpoint's sentence.**

1. **Nothing wires trigger → score → target.** `runtime.cycles.evaluate` takes the
   evaluator as a seam, and no task in this plan fills it. T9's acceptance was the
   ingress, the idempotency and the asymmetry; T7's was the freeze. Connecting them was
   never assigned. Same shape as the `MemoryStore` gap found at Checkpoint A: the
   checkpoint asserts something no task builds.
2. **"A real cohort" is still stand-in scores.** `data/scores/` is empty pending a
   `TABPFN_TOKEN` and `scripts/record_scores.py` (T6).

Neither blocks Phase 4 — T11 (`PRE_COMMIT`) and T12 (Ollama) depend on neither, and
T13 is the natural place for the wiring since it is the first task that needs a cohort
to draft *from*. **Decided: the wiring folds into T13**, which is the first task that needs a cohort to
draft *from*. Checkpoint C's first line stays open until then, deliberately.

---

## Phase 4 — The model drafts, the registry records

- [x] **T11 — Give `PRE_COMMIT` real semantics** · layer 8 · *S*
  - Acceptance: `PRE_COMMIT` grants a policy approval — audited with the rule that
    granted it, no human woken. `ALWAYS` still requires a human.
  - Verify: new `tests/fitness/test_approval_tiers.py` — criterion 20. 15 tests.
  - Depends: none. Files: ~3.
  - **Done.** The defect was three tiers with two behaviours: `PRE_COMMIT` and `ALWAYS`
    both demanded a human-granted record, so the middle tier was a comment.
  - **The invariant that mattered most runs the other way:** a policy grant must never
    satisfy `ALWAYS`. The record it mints matches the run, the fingerprint and the state
    snapshot — every check the old code made — so without an explicit source check a
    rule could authorise an irreversible act, and the strongest tier would become the
    easiest to satisfy. Sabotage-verified.
  - `PreCommitPolicy` **defaults to refuse** and its checks are conjunctive. A policy
    that can only say yes is a tier with a nicer name, which is what this was. An empty
    check set permits nothing rather than everything.
  - `migrations/0006` puts `granted_by` and `rule` on the record, with a CHECK that a
    policy grant names its rule and a human grant does not — a human approval carrying a
    rule name is a policy grant wearing a person's name.
  - **A bug the tests caught:** Postgres returns `granted_by` as a plain string, and
    `granted_by is GrantedBy.HUMAN` is False for one however equal it compares. Every
    stored human approval would have read as a policy grant. `ApprovalRecord.of` coerces.

- [x] **T12 — Ollama model engine adapter** · layer 4 · *M*
  - Acceptance: a `ModelEngine` over a local Ollama model, emitting tool proposals.
    `num_ctx` explicit and recorded in Foundation Assumptions.
  - Verify: new `tests/fitness/test_ollama_contract.py` — a malformed proposal still
    produces `tool.reject` and an answered turn. 18 tests, plus 5 in `tests/live`.
  - Depends: none. Files: ~3.
  - **Done.** `qwen3:8b` on Ollama 0.34.3, installed and verified emitting a correctly
    typed tool call before any code was written — `amount_cents` came back as an int,
    which matters because the schema validator refuses a string and every refund would
    otherwise take the reject path.
  - **The contract had to change:** `ModelRequest.exposed_tools` carried only names, so
    native tool calling had nothing to send. It now carries `ExposedTool` — name,
    description, parameter schema — and *not* the scope, surface, approval tier or
    idempotency policy. Layer 6 sits above layer 4, so the model package cannot import
    the registry, and it should not want to: handing over a `ToolSpec` would hand the
    model the authority metadata to reason about.
  - Three settings are decisions, not defaults: `num_ctx=8192` (Ollama's default is
    smaller than people assume), `think=False` (qwen3's monologue would land in the text
    a human is shown), `temperature=0` (criterion 17 wants the same inputs to give the
    same experiment).
  - The adapter **filters nothing**. A tool that was never offered and arguments of the
    wrong type are both reported faithfully; the exposure filter and the schema
    validator refuse them a layer up, where the refusal leaves evidence.
  - **Found a hole in `stack_guard` while doing this:** `git diff HEAD` says nothing
    about untracked files, so a brand-new module full of suppressions passed the loop's
    own gate. CI compares commits and would have caught it, but the hook had already
    said yes. The guard now scans untracked files; proven with a probe file.
  - Ratchets: fitness tests 29 → 30; project coverage 99% → 98% (two lines constructing
    the real Ollama client, exercised in `tests/live`).

- [x] **T13 — Candidate drafting tool and registry surface** · layers 6, 7 · *M*
  - Acceptance: `create_experiment_draft` (side-effecting, reversible, `PRE_COMMIT`) and
    `roll_out_variant_to_percentage` (irreversible, `ALWAYS`, carrying the cohort
    predicate). Registry client in `execution/surfaces.py` and nowhere else.
  - Verify: criterion 19 — a proposal with a missing field, wrong type, extra argument
    or unexposed tool is refused before the registry is touched. 23 tests.
  - **Also carried the Checkpoint C wiring**, now done: `runtime/operator.py` fills the
    evaluator seam — trigger → load snapshot → score → target → frozen cohort or a
    recorded reason. 11 tests.
  - **Deviation:** the plan said "replace the placeholder catalog". The refund pair is
    kept and the experiment pair added on its own stage. Those two tools are the CLI
    walkthrough and ~20 fitness tests proving layer invariants; deleting them destroys
    working evidence for no gain, and separate stages are the exposure filter doing
    exactly its job — a drafting run is never shown the rollout tool.
  - The wiring **stops before drafting**. Drafting is the model's job and happens in a
    turn through the exposure filter, policy and the gateway; this produces the cohort a
    draft is written *about*, because the point of deterministic targeting is that no
    model gets a say in who enters the experiment.
  - Idempotency keys are the identity of the effect: the rollout percentage is in the
    key, so 10% and 25% are two effects and widening a rollout cannot be swallowed as a
    retry of the first.
  - **Found another guard gap:** the approval-downgrade check read one hardcoded
    filename, so `experiments.py` was unwatched — a rollout could go `ALWAYS` →
    `PRE_COMMIT` silently. It now watches every file under `tools/`.

### ✅ Checkpoint D — untrusted model output reaches a real side effect safely
- [ ] A model-drafted candidate is validated, policy-checked and written
- [ ] A malformed one is refused, traced, and answered
- [ ] The write produces a `PRE_COMMIT` audit record naming the rule
- [ ] Human review

---

## Phase 5 — Approval through Slack

- [x] **T14 — Slack outbound** · layer 1 · *M*
  - Acceptance: post the rendered `ApprovalPrompt` as an interactive message with the
    headcount and dollar figure.
  - Verify: criterion 15 — the message names estimated customers affected, not a bare
    percentage. 29 tests.
  - Depends: T13. Files: ~3.
  - **Criterion 15 is structural, not a formatting convention.** `ApprovalAsk` cannot
    be constructed without `estimated_customers`, so a later edit cannot quietly drop
    it. The headcount is in the *headline* and in the notification fallback text,
    because the headline is what a phone shows and what someone decides from without
    opening the app.
  - An ask reaching **zero** customers is refused: waking someone for nothing teaches
    them the asks do not matter.
  - **A failed notification is loud.** A run parked on a question nobody received waits
    forever and looks exactly like one waiting patiently. Slack accepting without
    returning a message id counts as failure too — accepted-but-invisible is worse than
    refused.
  - The buttons carry `(run_id, wait_id, experiment_version, data_as_of)`, so T15's
    reply comes back bound to the exact action and frozen cohort rather than to
    whoever clicked.
  - Slack is a **channel, not an execution surface**: it carries a question out and
    changes no business state, and routing the approval request through the approval
    gateway would be circular. Uses `slack_sdk`, same reasoning as the Ollama client.
  - **Coverage caught dead code:** `_message_id` was defined and never called — a
    `ruff format` reflow had made my edit miss, leaving the inline version in place.

- [x] **T15 — Slack inbound and transport authentication** · layer 1 · *M*
  - Acceptance: signature over the raw body, timestamp freshness, replay rejection. The
    adapter passes the user id onward as a **claim** and decides nothing with it.
  - Verify: criterion 24 — bad signature, stale timestamp and replayed body each
    refused before any policy runs. 34 tests.
  - Depends: T14. Files: ~3.
  - **Three checks, each catching what the others cannot.** A signature proves the
    bytes came from Slack; freshness proves they are recent; the replay guard proves
    this is the first time. The second copy of an "Approve" is not a second decision.
  - **Order is load-bearing.** Freshness first (cheap, drops stale floods without
    hashing), signature second, **replay last** — recording an unverified signature
    would let anyone fill the table by posting garbage, turning a defence into a
    denial-of-service surface. A test asserts a forged signature is never recorded.
  - Verification strictly precedes parsing: parsing first would mean acting on the
    shape of bytes nobody authenticated.
  - The replay guard is in Postgres (`migrations/0007`), not a per-process set: a
    replay landing on a different worker is exactly the case it exists for.
  - **A contract collision worth recording.** `lint-imports` forbids `urllib` to layer
    1, and `urllib.parse.parse_qs` tripped it. The rule means "no network client", but
    import-linter rejects `urllib.request` as a forbidden module ("subpackages of
    external packages are not valid"), so it cannot be written narrowly. Widening it to
    admit a URL *parser* would weaken a real constraint for a tooling limitation, so
    the form decoding is ten hand-rolled lines instead — tested against `urllib` itself
    in a file where importing it is allowed.
  - `MalformedCallback` is deliberately **not** a `CallbackRefused`:
    authentic-but-unintelligible and forged are different problems, and collapsing them
    loses the distinction in the audit trail.

- [x] **T16 — Approver authorisation** · layer 8 · *S*
  - Acceptance: approver-group membership checked in policy, scoped to the acting
    tenant. Tenant A's rollout is not approvable by tenant B's people.
  - Verify: criterion 25 — a correctly-signed interaction from an outside user is
    refused in layer 8, not in the adapter. 16 tests.
  - Depends: T15. Files: ~3.
  - **The load-bearing detail is where the tenant comes from.** It is resolved from the
    run the approval is bound to, never from the interaction. "This is really from
    Slack" and "this person is an approver" are both correct checks that compose into a
    confused deputy if the attacker names the tenant they are checked against.
    `ApprovalReply` carries no tenant at all, so there is nothing to be tempted by.
  - Default deny is the **primary key's** doing — `(tenant, slack_user_id)` — not a
    branch someone has to remember. Standing in two tenants takes two rows.
  - `principal` is stored separately from the Slack id so "who approved this" stays
    answerable after someone leaves and their id is recycled. Tested.
  - The group records **who added each member**: a group nobody can be shown to have
    changed is one that can change unnoticed.
  - Two tests hold the layer boundary from both sides: the adapter does not import the
    directory, and the directory contains no transport vocabulary.

- [x] **T17 — Approve → resume → roll out** · layers 3, 7 · *M*
  - Acceptance: a Slack approval grants an `ApprovalRecord` bound to
    `(run_id, fingerprint, experiment_version, data_as_of)`, satisfies the wait, and
    the rollout commits exactly once through the gateway.
  - Verify: criterion 23 — the full path with the process killed while parked. 11
    fitness tests + 4 in `tests/durability/test_approve_after_death.py`.
  - Depends: T16, T10. Files: ~4.
  - **The binding was already structural.** `ActionRequest.fingerprint()` covers the
    payload, and T13 made `experiment_version`, `targeting_model_version` and
    `risk_threshold` required rollout arguments — so a different frozen cohort is a
    different payload, a different fingerprint, and an approval that simply does not
    match. Criterion 5 needed no new machinery, only the observation.
  - **The real gap only appears across a process death.** The process that parked the
    wait held the prepared request and the rendered prompt in memory; the process
    handling the click an hour later holds neither, and cannot bind an approval to a
    fingerprint it never computed. `migrations/0009` puts both on the wait, with a
    CHECK that an approval wait has them.
  - `grant_for_fingerprint` exists so the answer path binds the fingerprint the wait
    **recorded**, never one recomputed at answer time — recomputing would approve
    whatever *that* process derives rather than what the approver was shown.
  - The coordinator **commits nothing**. The next turn does, through the gateway, which
    keeps the commit on the one path with idempotency, containment and an audit record.
  - **Contract 4 caught the same design error as T9:** `runtime` cannot import the
    channel layer, so `ApprovalReply` moved to `policy/approvers.py` beside the code
    that authorises its claim. It carries no tenant — that comes from the run.

### ✅ Checkpoint E — the end-to-end path runs
- [ ] Trigger → score → target → draft → registry → Slack → approve → rollout
- [ ] Killed while parked, resumed, still correct
- [ ] Stale approval refused; unresolved effect not retried blind
- [ ] Human review

---

## Phase 6 — Production hardening

- [x] **T18 — Wait deadlines and stalled detection** · layer 3 · *M*
  - Acceptance: every `trigger` wait carries a deadline; `operator stalled
    --older-than` reports runs past it; the approval re-ask timer fires.
  - Verify: criterion 4 — `tests/fitness/test_stalled_waits.py`, 26 tests.
  - Depends: T17. Files: ~3.
  - **Done.** `deadline` has one meaning for every kind: when the wait should have been
    satisfied by. Past it the remedies differ — a trigger wait is *stalled* and
    reported, an approval is *re-asked* — and neither may lapse. `migrations/0010`
    holds it in the database too (`pending_waits_have_a_deadline`), and backfills any
    pending wait parked before it as **due now**: a future deadline invented for a wait
    nobody was watching would hide exactly the waits this is for.
  - **A trigger wait has no default timeout.** The right one is "a little longer than
    the data normally takes", which only the caller knows. An approval defaults to 24h:
    how patiently to treat a person does not depend on the workflow. `park` refuses a
    trigger wait without one, so T17's own test had to name one — the invariant caught
    its first caller on the first run.
  - `--older-than` **narrows, never widens**: the deadline decides what is stalled, age
    only filters the report. The CLI exits 1 when anything is stalled, so a scheduler
    alerts on the exit code rather than on someone reading output.
  - **The re-ask asks first, then moves the deadline.** Dying between the two asks
    twice; the other order loses the question for a whole interval. A duplicate is
    harmless (the second answer is refused as already answered); a missing one is the
    silent expiry. A failed ask is raised *after* the others are asked, with its
    deadline left where it was. `record_reask` is conditional on the deadline it read,
    so two timers can both ask but only one moves it — T21 territory.
  - **Named gaps.** The asking is a seam (`fire_reasks(ask=...)`): runtime cannot
    import the channel (contract 4), and nothing yet builds an `ApprovalAsk` *from a
    wait* — the wait carries the summary and fingerprint, not the headcount, cohort and
    dollar figure Slack needs. That is the same wiring as the *first* ask, which
    Checkpoint E still owes. No production path parks a `trigger` wait yet either; the
    store and the database refuse one without a deadline whenever one appears. The
    command is `agentstack-operator`, not the spec's bare `operator`, matching
    `agentstack-migrate`. Unsettled `trigger_cycles` are not reported yet.

- [x] **T19 — Checkpoint versioning and `needs_migration`** · layer 3 · *M*
  - Acceptance: every checkpoint carries a schema version; an incompatible one parks
    the run in `needs_migration`, distinct from `stalled`, and never resumes into a
    shape it does not understand.
  - Verify: criterion 21 — `tests/fitness/test_checkpoint_versioning.py`, 12 tests.
  - Depends: T8b. Files: ~3.
  - **Done.** `schema_version` is a key of `TurnState`, stamped when a turn starts, so
    every checkpoint holding state carries it (LangGraph's first, pre-input checkpoint
    holds nothing). `CHECKPOINT_SCHEMA_VERSION` and `COMPATIBLE_SCHEMA_VERSIONS` in
    `runtime/graph.py` say what this code writes and what it can still resume.
  - **The check is in `advance`, before LangGraph is invoked**, so nothing - not
    `check_waits`, not the model - runs against a misread state. It applies to *any*
    existing checkpoint, not only an unfinished one: invoking a finished thread with
    new input merges into the old values, which leaks the old shape just as surely.
  - **An unversioned checkpoint is incompatible, not "probably v1".** Pre-T19
    checkpoints of unfinished turns park on first touch. ADR-0005's rule: noticed,
    never guessed.
  - **`needs_migration` is a wait kind**, not a new table. Waiting is state (Part 4),
    and a parked run is waiting for a migration: the wait blocks *every* turn of the
    run through the existing gate, survives the process, and is released by the
    existing, audited resume. Its state snapshot names `thread@checkpoint: schema vN`,
    so a resume has to identify what was migrated. Parking is idempotent per
    checkpoint. `NeedsMigration` is raised *after* parking — the exception is this
    process failing loudly (criterion 2), the wait is what everyone else sees.
  - `operator stalled` reports it on its own line and in its own count, with no
    deadline: it was never going to resume by waiting.
  - **Named gaps.** No checkpoint migrations exist yet; the remedy is proven in a test
    by rewriting the version with `update_state`, which is what a real migration would
    do plus reshaping the values. Nothing yet checks that `TurnState` or the node set
    changed *without* a bump — that is T20's CI guard, and it needs a shape fingerprint
    to compare. Two processes touching the same incompatible checkpoint at once can
    both park (check-then-insert); harmless, both block, and T21's.

- [x] **T20 — CI guard against a stranding deploy** · layer 10 · *S*
  - Acceptance: CI compares the checkpoint schema against the last release and fails an
    incompatible change that ships no migration note.
  - Verify: criterion 22 — a deliberate incompatible change fails the build.
    `tests/fitness/test_checkpoint_guard.py`, 16 tests, plus a real edit to `TurnState`
    run against the committed record (below).
  - Depends: T19. Files: ~2.
  - **Done.** `graph.checkpoint_shape()` is the shape as data: `TurnState`'s keys with
    their source-text types, and the graph's nodes. Edges are left out on purpose — a
    checkpoint resumes at the node it names, so rewiring after it strands nothing.
    `checkpoints/schema.json` records it; `scripts/checkpoint_guard.py` judges.
  - **"The last release" had to be defined, because there are no tags.** It is the
    record as it stands at `--base`: the PR's base branch in CI, the pre-push commit on a
    push to `main`, `HEAD` locally, a tag once releases are tagged. Comparing against
    the base rather than the worktree is what makes the check honest: a change that
    rewrites the record to match itself still fails against the release it ships over.
  - **Compatible vs not is decided mechanically.** Adding a key (`total=False`, reads as
    absent) or a node passes. Removing or retyping a key, or removing or renaming a
    node, fails unless the change bumps the version, **drops the old one from
    `COMPATIBLE_SCHEMA_VERSIONS`** (otherwise T19 resumes those runs into the new shape
    instead of parking them), and adds a non-empty `checkpoints/vN.md`.
  - The record must also match the code, so the next release is compared against the
    truth; `--write` regenerates it and never makes a breaking change pass. An unknown
    `--base` exits 2 instead of reading as "nothing released, all clear" — a typo in
    CI's ref must not be a pass.
  - Wired into `check_task.sh`, CI and a new `CONSTRAINTS.md` row.
  - **Named gaps.** Types are compared as source text, so `Optional[str]` →
    `str | None` would read as a retype: a false alarm, never a miss. A change to what
    a key *means* with the same name and type is invisible to any structural check;
    that stays a reviewer's judgement, and the note in `checkpoints/README.md` says so
    by listing what a migration note must contain.

- [x] **T21 — Concurrency** · layers 2, 3, 10 · *M*
  - Acceptance: trigger fan-out without stampede, pooling sized for the target, 100
    concurrent runs verified. Record the number actually achieved.
  - Verify: `tests/durability/test_concurrency.py` (7 tests) and a regression test in
    `tests/infra/test_migrations.py`.
  - Depends: T17. Files: ~4.
  - **The T15 flake is explained, reproduced every time, and fixed.** It was not pool
    sizing. `migrate.apply` read its ledger and left that transaction open, so every
    per-file `conn.transaction()` after it was a *savepoint*, and nothing committed
    until `_exclusive.__exit__` — which unlocked **before** committing.
    `pg_advisory_unlock` takes effect when it executes, so for one round trip the
    second migrator held the lock, could not see the uncommitted `0001`, ran it again
    and failed with "relation already exists". One round trip is why it needed a
    loaded, coverage-traced suite to show. Sleeping 0.3s after the unlock reproduced
    it every time (`DuplicateTable`); the fix commits the read and commits before
    unlocking. The regression test drives the real `__exit__` through a connection
    class that records the transaction state at unlock (`INTRANS` before, `IDLE`
    after) and fails on the old code. "One transaction per file" is now true, not
    approximately true.
  - **Found by the load test: a raced step crashed its loser.** Two deliveries of one
    run's resuming turn both passed `StepLedger.step`'s `completed` check. The effect
    was still applied once (the idempotency ledger hands the loser the winner's
    receipt), but the loser died on `run_steps_complete_once` — in 80 of 100 runs.
    Now a complete-once conflict with the *same* receipt is "another worker finished
    this step"; a *different* receipt raises `StepConflict` (two effects); any other
    constraint still raises. `started_but_unfinished` no longer reports a step that
    has completed just because the loser's `started` row came after the winner's
    `completed`. All three step tests fail on the old ledger.
  - **Fan-out:** `runtime/fanout.py` drains a batch through at most `max_in_flight`
    workers (default 8, never above the pool). Duplicates are already settled by the
    cycle claim; one failed evaluation is returned, not raised, and stays unsettled.
    Verified: 100 experiments × 3 deliveries, shuffled → 100 evaluations, peak ≤ 8.
  - **Numbers achieved** (laptop, Apple silicon, Postgres 16 in Docker, one process,
    full refund → park → approve → resume → commit path, each resume delivered twice
    at once, every run committing exactly once):

    | Runs | App pool | Checkpointer pool | Wall | Peak server conns | Pool timeouts |
    |---|---|---|---|---|---|
    | 100 | 5 | 2 | 2.5s | 7 | 0 |
    | 100 | 10 | 4 | 2.6s | 12 | 0 |
    | 100 | 20 (default) | 4 (default) | 2.7s | 22 | 0 |
    | 100 | 40 | 4 | 2.6s | 27 | 0 |
    | 200 | 20 | 4 | 5.8s | 22 | 0 |
    | 300 | 20 | 4 | 9.3s | 22 | 0 |

    **Wall time does not move with pool size**: one process is bound by Python, not
    Postgres, and the checkpointer never opened more than 2 connections. The defaults
    stand and are sized with room; at 24 connections a process, `max_connections=200`
    admits 8 worker processes. Scaling past one process is more processes, not a bigger
    pool. The suite verifies 100 against its own smaller pools (app 8, checkpointer 4).
  - **Open decision, not made here: a per-run lease.** A duplicate that reaches the
    gateway while the winner's claim is still open is refused with `UnresolvedEffect`
    and audited `effect.unresolved` — 8–33 per load run. Safe (never a second effect),
    but *misleading*: the claim is live, not abandoned, and an operator would reconcile
    something that needs nothing. The fix is to stop two turns of one run overlapping
    at all (a lease row on `runs`), which forces a lease TTL: longer than any turn, and
    it delays resume after a process death by up to that TTL — which `tests/durability`
    would then have to model. That trade-off is the user's.
  - The T18/T19 races noted earlier (two timers re-asking, two processes parking one
    checkpoint) remain harmless by construction: both are idempotent in effect.

### ✅ Checkpoint F — iteration 1 complete
- [ ] All 28 success criteria in `SPEC.md` met or explicitly deferred with a reason
- [ ] The `live` suite has been run at least once against the real model
- [ ] Fitness tests and gates green; ratchets held
- [ ] `/stack-audit` run and its findings addressed
- [ ] The two named debts still named: PII in traces, secrets in environment variables

## Phase 7 — Experiment registry and narrow registry tools

Spec: `SPEC-registry.md`. Plan: `tasks/plan.md` § Phase 7. Every task names its layers
and the test that proves it.

- [x] **T22 — Split the experiment stage** · layers 6, 3 · *S*
  - Acceptance: `EXPERIMENT_STAGE` replaced by `draft` / `evaluation` / `rollout`;
    `DRAFT` on `draft`, `ROLLOUT` on `rollout`; `migrations/0011` remaps existing
    `runs.stage='experiment'` rows to `draft`.
  - Verify: new `tests/fitness/test_registry_tools.py` — a `draft` run is not shown
    `roll_out_variant_to_percentage`; full fitness suite and `checkpoint_guard` green.
  - Files: `tools/experiments.py`, `migrations/0011_*`, `test_experiment_tools.py`,
    `test_approve_resume_rollout.py`, `tests/durability/park_worker.py`.
  - **Done.** 6 tests: the exact stage × tool matrix (`test_registry_tools.py`) and the
    remap up and down (`tests/infra/test_experiment_stage_migration.py`). The down
    migration is lossy by necessity — three stages fold back into one — and says so.
  - The migration test clears `audit` and `langgraph` on the way in *and* out: the
    shared infra fixture resets only `public`, so a real-migrations test that leaves
    them behind fails the next one on `DuplicateSchema`.
  - **Found, not fixed:** two suites sharing the dev Postgres corrupt each other's
    fixtures (duplicate approver keys, sessions vanishing mid-test). A second session
    ran `check_task.sh` concurrently and produced 20+ spurious failures; alone, the
    suite is green. Per-invocation test database names would fix it — not this task.

- [x] **T23 — Registry schema** · layer 10 · *S*
  - Acceptance: `migrations/0012` — `experiments` (status CHECK over five states),
    `experiment_versions`, `draft_revisions`, `registry_events` (unique
    `idempotency_key`); history tables insert-only by trigger.
  - Verify: `tests/infra/test_migrations.py` up/down/up; `tests/infra/test_registry_store.py`
    refuses UPDATE/DELETE on history tables.
  - **Done.** 15 tests in `tests/infra/test_registry_store.py`, straight at the schema:
    closed lifecycle, versions that must exist, insert-only history (UPDATE and DELETE,
    per table), one row per effect, tenant in every key. `0012` down/up is exercised
    by the T22 migration test's rollback to 10.
  - **Spec revised** (`SPEC-registry.md` § Store): four states, not five; no
    `idempotency_key` column — the surface never receives the key, so the store holds
    unique what the key is made of; no run/approval columns — that is the audit trail.
  - **Test isolation, again.** Another session's suite dropped `agentstack_app_test`
    mid-run (95 spurious failures). Verified instead against a throwaway Postgres on
    port 5434 (`DATABASE_URL=…:5434/agentstack`): 570 passed. The fix — a per-invocation
    test database name — is now worth a task of its own.


- [x] **T24 — Surface refusal semantics** · layers 7, 3 · *S*
  - Acceptance: `SurfaceRefused` → gateway `abandon`s the claim and audits `refused`;
    the turn gets a tool refusal. A lost answer still leaves `IN_FLIGHT`.
  - Verify: `tests/fitness/test_unresolved_effects.py` (extended).
  - **Done.** 5 tests in `test_unresolved_effects.py`: a refusal releases its claim,
    is audited `refused` / `surface.refused` (never `unresolved`), is answered by the
    turn as a `tool.reject`, re-raises to a caller without a turn, and leaves the key
    free so the later legitimate call acts exactly once.
  - `SurfaceRefused` is the only exception the gateway abandons a claim for, and its
    docstring says who may raise it: a surface that can *prove* nothing applied. Every
    other failure after dispatch still leaves the claim `IN_FLIGHT`.
  - `RecordingClient.refuse_before_effect` mirrors `fail_after_effect`, so both halves
    of the distinction are testable against the same client.

- [x] **T25 — `PostgresRegistryClient` for the two existing tools** · layer 7 · *M*
  - Acceptance: single-statement guarded writes via `Database`; the fake enforces the
    same preconditions; `wiring.py` uses the Postgres client.
  - Verify: one contract suite over both clients (spec criterion 7);
    `test_approve_resume_rollout.py` and `tests/durability` green.
  - **Done.** 21 contract cases (`tests/infra/test_registry_store.py`), each run against
    the fake and against Postgres, plus a fitness check that `build_stack` wires the
    real client. Drafts round-trip; a rollout needs an experiment at the named current
    version in `draft` or `live`; widening is a second effect, repeating one is refused;
    unknown resources are refused; tenants are separate registries.
  - Each write is one statement: an `ON CONFLICT DO NOTHING` insert chain for a draft,
    a guarded `UPDATE … RETURNING` feeding the event insert for a rollout. No row back
    is `SurfaceRefused`, which T24 turned into a released claim.
  - **Decided narrow, not in the spec:** drafting an experiment id that already exists
    is refused. Redrafting a halted or discarded experiment under a new version is
    assumption 6's relaunch path, and no tool owns it yet — letting the draft tool do it
    would widen its blast radius to every existing experiment. Needs a decision before
    anyone relaunches.
  - Two fixtures now seed the draft an earlier turn would have written: the real
    registry refuses to roll out an experiment nobody drafted, which the fake never did.

### ✅ Checkpoint G — real store, behaviour unchanged
- [ ] `check_task.sh`, `tests/durability`, `lint-imports`, `stack_guard`, `checkpoint_guard` green
- [ ] Human review

- [ ] **T26 — Read tools** · layers 6, 7, 5 · *M*
  - Acceptance: `get_experiment`, `list_experiments` (`limit` 1–50), `get_rollout_history`.
  - Verify: matrix rows in `test_registry_tools.py`; `limit: 51` refused; read of
    model-authored text enters context `UNTRUSTED` (`test_untrusted_content.py`);
    `execution.read` spans (`test_trace_completeness.py`).

- [ ] **T27 — Draft lifecycle** · layers 6, 7 · *M*
  - Acceptance: `revise_draft_hypothesis`, `discard_experiment_draft`; revise appends a
    revision, never a new `experiment_version`.
  - Verify: store contract — refused from `discarded`, `live`, `halted`; lost answer → one row.

- [ ] **T28 — `record_abstention`** · layers 6, 7 · *S*
  - Acceptance: append-only, no status change, `evaluation` stage only.
  - Verify: store contract — appends in every state, status unchanged.

- [ ] **T29 — `halt_rollout` + `halt_only_zeroes`** · layers 6, 7, 8 · *M*
  - Acceptance: `live → halted` only; no `percentage` argument; policy refuses a
    non-zero halt payload before the surface.
  - Verify: `test_approval_tiers.py` (spec criterion 6); store contract.

### ✅ Checkpoint H — all nine registry tools
- [ ] Exposure matrix exactly draft 5 / evaluation 5 / rollout 4 (spec criterion 2)
- [ ] Human review

- [ ] **T30 — Narrowness bar, concurrency, injection eval** · layers 6, 9 · *M*
  - Verify: `test_registry_tools.py` narrowness rules; 20 concurrent halts → one
    `halted`, one event (`tests/durability/test_concurrency.py`); injection case in
    `evals/cases/`.

- [ ] **T31 — Ledger and bar** · docs · *S*
  - `CONSTRAINTS.md` gains "Registry narrowness" and "Registry preconditions" rows
    (additions only); `STACK.md` rows 6/7; `SPEC-registry.md` migration numbers and
    open-question answers; `SPEC.md` decisions table links the sub-spec.

## Phase 8 — Durable runtime on Temporal

Spec: `SPEC-durable-runtime.md`. Plan: `tasks/plan.md` § Phase 8. **Depends on Phase 7
being complete.** Every task names its layers and the test that proves it. Criteria
numbers (C29–C45) are the spec's; SPEC.md criteria 1–28 must still hold after every task.

- [x] **T32 — Temporal substrate, verified first** · layer 10 · *S*
  - Acceptance: a `temporal` service in `docker-compose.yml` with its own database;
    `dev_up.sh` waits for it; `temporalio` moves from the `spike` group to runtime
    dependencies; a test fixture uses per-invocation task queues. **Verify first:**
    coverage.py either sees sandboxed workflow code or the unsandboxed-runner fix is in
    place.
  - Verify: new `tests/infra/test_temporal_substrate.py`: Temporal down → the test
    **fails** (C45); a one-line sandboxed workflow shows up in the coverage report.
  - Files: `docker-compose.yml`, `scripts/dev_up.sh`, `pyproject.toml`, `tests/conftest.py`,
    `tests/infra/test_temporal_substrate.py`.
  - **Done**, in a separate worktree (`feat/temporal-t32`) while Phase 7 is still being
    built. 4 tests. **The risk is closed:** coverage.py records lines that run inside
    the workflow sandbox. The test was also checked against a planted line that never
    runs, and it fails on that one.
  - **The spec's dev server was wrong.** `temporalio/auto-setup` stopped at server
    1.29.7. The spike verified 1.32.0. The dev server is now the CLI image
    `temporalio/temporal:1.9.1` (`server start-dev`, SQLite on a volume), and the spec's
    Tech Stack row is amended to say so.
  - The image runs as the unprivileged `temporal` user, so its volume mounts at
    `/home/temporal`, the one directory it owns. A fresh named volume elsewhere comes
    up root-owned, and the server dies with `unable to open database file`.
  - `docker-compose.yml` now pins `name: revenuecat-agent`. From a second checkout,
    compose would otherwise start a second project whose fixed container names
    collide with the first.
  - CI starts Temporal with `docker compose up -d --wait temporal`. It isn't a
    service container because those can't pass `server start-dev`.
  - No async test plugin: each test drives its own `asyncio.run`. Adding
    `pytest-asyncio` is a dependency decision, and this task didn't need it.
  - **Moved to T38:** pinning the time-skipping test server in CI. Nothing uses it
    before T38.
  - **Found, not fixed:** `.gitignore` ignores all of `data/`, so
    `data/manifest.json` is **not** committed, despite CLAUDE.md saying it is. A fresh
    checkout fails `test_data_snapshot` until the manifest is copied in.

- [x] **T33 — Workflow package skeleton + contract 6** · layer 3 · *S*
  - Acceptance: `runtime/temporal/contracts.py` (ids-only dataclasses) and an empty
    `ExperimentWorkflow`; `.importlinter` contract 6 exactly as in the spec.
  - Verify: `lint-imports` green; new `tests/fitness/test_temporal_boundaries.py`:
    contract 6 exists and names all six forbidden modules (C38); `stack_guard`
    flags its removal.
  - Files: `runtime/temporal/{__init__,contracts,workflows}.py`, `.importlinter`, test.
  - **Done.** 5 tests. Besides the contract, the workflow side is also held to an
    allow-list (stdlib, `temporalio`, `contracts`), which a layer added later can't
    slip past. The empty workflow runs on the dev server.
  - **Contract 6 held at lint time and failed at runtime.** The sandbox re-imports a
    workflow's parent packages, and `agentstack/runtime/__init__.py` re-exported
    `run_turn`, `WaitStore` and others, so importing `workflows` loaded the gateway and
    `requests` inside the sandbox, which refused them. Nothing imported those
    re-exports, so the init now imports nothing. Passing modules through the sandbox
    would have worked too, and it would have switched off determinism checks on our
    own workflow code.
  - T32 was cherry-picked onto this branch (it was based before T24).

- [x] **T34 — Worker, entry point, run identity** · layer 3 · *M*
  - Acceptance: `worker.py` + `agentstack-worker`; the workflow id is
    `experiment-run:{run_id}`; an `ensure_run` activity upserts the `runs` row; the worker
    runs preflight before polling and exits non-zero when Temporal is unreachable.
  - Verify: `test_temporal_boundaries`: 5 concurrent starts → one workflow, one `runs`
    row (C29); worker with no server exits non-zero (C45); `test_run_identity`,
    `test_prediction_gate` green.
  - Files: `runtime/temporal/{worker,activities}.py`, `interfaces/worker_cli.py`,
    `pyproject.toml` (script), test.
  - **Done.** 9 tests in `test_temporal_boundaries`. `start_run` (in `client.py`) attaches
    to a run that's already going (`USE_EXISTING`) rather than failing, so all five
    racing starts get the same handle, and the server decides it. `RunStart` carries the
    run's identifiers (session, tenant, user, stage, channel) and nothing that grants
    authority.
  - Preflight is an injectable argument that defaults to the real `agentstack-preflight`.
    One test proves the default refuses without a token. The C45 test injects a passing
    preflight, so it fails for the reason it's about (no server), and it's bounded at 5s.
  - **Seen before T35 fixed it:** the first run of the test hung. `runs.session_id` is a
    foreign key, the test had no session, and Temporal's default policy retried the
    failing activity forever. That's rule 2's reason for existing, and why every
    result wait in the tests is now bounded.
  - Not yet covered: `worker_cli._serve`, the poll loop itself (78% of the file).
    Covered at T36 (see there).

- [x] **T35 — The one RetryPolicy + declared-activity interceptor** · layer 3 · *S*
  - Acceptance: `retry.py`: refusals (`UnresolvedEffect`, `ApprovalStale`,
    `ApprovalRequired`, `PolicyDenied`, `SandboxViolation`, `ApproverNotAuthorized`,
    `SurfaceRefused`) are non-retryable; `interceptors.py` refuses undeclared activities.
  - Verify: `test_temporal_boundaries`: each refusal type → exactly one attempt (C35);
    an undeclared activity is refused non-retryably (C37).
  - Files: `runtime/temporal/{retry,interceptors,worker}.py`, test.
  - **Done.** 14 tests in the file now. The policy holds type **names**, because the
    workflow uses it and contract 6 forbids it a path to `execution` or `policy`. The
    test resolves each name to the real class, so a rename fails the build instead of
    turning a refusal back into a retry. `retry.py` joins the workflow side's
    allow-list test.
  - Checked against a mutant: with the refusals dropped from the list, the test fails.
    The seven refusals spin until the bounded wait gives up.
  - An AST test makes every `execute_activity` in `workflows.py` pass `RETRY`, so
    there's only one policy.
  - `DECLARED` is separate from what's registered, which is E4's whole point. A test
    registers `rogue_rollout` on a worker with the guard: `UndeclaredActivity`,
    non-retryable, body never ran. Another asserts the production worker installs the
    guard and declares everything it registers.
  - **Decided at Checkpoint I (human, 2026-09-27):**
    (a) `OutcomeNotAuthorized` goes on the list in T36. Left retryable, the second
    attempt finds the cycle already claimed and returns it unsettled. The activity
    then *succeeds*, and the refusal only survives in Temporal's history.
    (b) **No attempt cap.** A cap can't tell an outage from a bug, and it would kill a
    weeks-long parked run over a short Postgres outage. Instead, `IntegrityViolation`
    is non-retryable (`retry.DETERMINISTIC`): stores that expect a conflict catch it
    themselves, so one reaching an activity is a bug. Anything else still retried
    indefinitely gets surfaced by `operator status` (T50).

### ✅ Checkpoint I — foundation, nothing can act yet
- [x] `check_task.sh`, `lint-imports`, `stack_guard`, `tests/infra` green; coverage risk closed
- [x] No activity exists that can reach `gateway.execute` (grimp: no import chain from
  `activities`, `worker` or `interceptors` to `execution.gateway`)
- [x] Human review (2026-09-27: both open questions decided, see T35)

- [x] **T36 — Trigger loop: an evaluation cycle as an activity** · layer 3 · *M*
  - Acceptance: the workflow waits for a trigger signal and runs `evaluate_cycle`
    (a thin wrapper over `cycles.evaluate` + `operator`); `metric_movement` still can't propose.
  - Verify: `test_trigger_asymmetry`, `test_trigger_to_candidate` green through the
    worker; the `evaluate_cycle` activity rerun after it wrote → one cycle.
    `OutcomeNotAuthorized` is non-retryable: exactly one attempt, and the workflow sees
    the refusal (decided at Checkpoint I).
  - Files: `runtime/temporal/{workflows,activities,contracts}.py`, `tests/durability/test_trigger_cycle.py`.
  - **Done.** 5 tests in `test_trigger_cycle`, all through a real worker except the
    direct rerun: a trigger settles one cycle; a trigger delivered three times scores
    once and leaves one `trigger_cycles` row; `metric_movement` is refused with
    `OutcomeNotAuthorized` and stays unsettled; a refused cycle doesn't end the run;
    the activity called twice converges.
  - **The run is open-ended now**, one per experiment (SPEC.md), so tests can't await
    its result. The `progress` query reports where it is (a query, so never history),
    and `tests/temporal_support.py` polls it with a bound. The T34 tests moved onto it.
  - Checked against a mutant: with `OutcomeNotAuthorized` off the list, attempt 2
    returns `outcome=None, refusal=None`. The refusal disappears into a success, which
    is exactly what Checkpoint I predicted.
  - `worker_cli._serve` is now covered by an in-process test that serves a run and is
    then cancelled (95% of the file). A real `agentstack-worker` process can't be the
    test harness, because it stops at preflight without TabPFN's weights. So the
    SIGKILL tests (T40, T44) run their own worker processes built on `build_worker`.
  - `Trigger` in `contracts` mirrors `TriggerEvent` as strings, because contract 6 keeps
    `policy` out of workflow code. The activity rebuilds the event, and the kind's
    authority is still only read by `cycles.evaluate`.
  - **Open, for T37:** nothing maps an experiment to its run yet. `deliver_trigger`
    needs a workflow id, and it has to start the run if there isn't one, which needs
    a session. Layer 1 may resolve neither.

- [x] **T37 — Ingress hands triggers to the workflow** · layer 1 · *S*
  - Acceptance: `interfaces/triggers.py` calls `client.deliver_trigger` (signal-with-start)
    and resolves nothing itself.
  - Verify: redelivered trigger **after the workflow closed**, with id reuse allowed →
    one evaluation, deduped on the Postgres claim (C30); `test_layer_boundaries` contract 4.
  - Files: `interfaces/triggers.py`, `runtime/temporal/client.py`, test.
  - **Done, with a design decided at T37 (human, 2026-09-27):** nothing mapped an
    experiment to its run, and a run can't start without a session. Migration `0013`
    adds `experiment_runs (tenant, experiment_id) → (run_id, session_id)`. The
    ingress does what `handle()` does for Slack, in the same place: `wiring.deliver`
    parses (`triggers.py` stays a pure parser), `run_for_trigger` resolves the run from
    the record, and `client.deliver_trigger` signal-with-starts it. The `RunStart`
    comes from the `runs` row, never from the trigger.
  - **Claim, then write.** The mapping is claimed with ids minted beforehand, and only
    the winner's ids are written (`SessionStore.ensure`, new, and `RunStore.ensure`,
    both upserts). Eight racing first deliveries leave one mapping, one session and one
    run, with no orphans, and a delivery that died halfway is finished by the next.
    No foreign key onto `runs`/`sessions`, because the claim comes first.
  - C30 against a real worker: deliver, terminate the workflow, redeliver. Temporal
    starts a **second execution** (asserted), the scorer still ran once, there's one
    `trigger_cycles` row, and the new execution reads back the first cycle's outcome.
  - The session belongs to `EXPERIMENT_OPERATOR` (`agent-operator`, as in
    `interfaces/cli.py`). The trigger's tenant only chooses which tenant's experiment
    is meant, the same authority it had before Temporal.
  - **`stack_guard` reports "2 assertions removed" in `test_trigger_cycle.py`.** It's a
    move, not a removal: the dataset fixture, with its two asserts, moved to
    `tests/durability/conftest.py` so both trigger modules share it. The counter works
    per file, and the net count across files is unchanged. Against `--base main` it's
    clean, because the file is new since `main`.

- [x] **T38 — Trigger waits and deadlines on durable timers** · layer 3 · *S*
  - Acceptance: a trigger wait writes its `waits` row (deadline required, as today) and
    the workflow's timer marks it due; `operator stalled` is unchanged.
  - Verify: `test_stalled_waits` green; time-skipping test: a wait past its deadline is
    reported by `operator stalled` (C43).
  - Files: `runtime/temporal/{workflows,activities}.py`, `tests/durability/test_timers.py`.
  - **Done, with the deadline's home decided at T38 (human, 2026-09-27):**
    `experiments/cadence.toml` (`default` 36h, assuming daily batches; `dev` 7d), read by
    `runtime/cadence.py` and passed to the activities, never read by the workflow. SPEC.md
    leaves the production cadence open, so `default` is written down as an assumption.
  - Whenever the run has no trigger to work on, it parks a `trigger` wait
    (`park_trigger_wait`). The row holds the deadline, and `operator stalled` is
    unchanged. The workflow's timer adds position only: `progress.overdue`. Nothing
    expires. A late trigger settles the wait (`satisfy_trigger_wait`), and the run moves
    on to its next one.
  - Rerun-safe: wait ids are `wait-{run_id}-trigger-{n}`, where `n` is workflow state
    that replay rebuilds (not a Temporal id, so E1 doesn't apply). `WaitStore.park`
    gains an optional `wait_id`, and parking an existing id returns the first wait with
    its first deadline. Satisfy checks before it resumes.
  - 6 tests in `test_timers.py`, three on the time-skipping server: 37 simulated hours
    make the run overdue, `operator stalled` reports its wait, a late trigger settles it
    and the settled wait drops out of the report; 72 simulated hours later nothing
    has expired. The other three cover the cadence file and the rerun of each activity.
  - **Found writing the C43 test:** once a trigger settles a wait, the run immediately
    parks its next one. `operator stalled` at "first deadline + 1 min" correctly
    reports that next wait too, since its deadline is a few real seconds later. The
    test now asserts the settled wait drops out, rather than asserting an empty report.
  - **CI:** the test server is fetched at the version the SDK pins
    (`temporal-test-server-sdk-python-1.33.0`), so `uv.lock` pins it, and the CI cache is
    keyed on `uv.lock`. A failed download fails the tests. It never skips them.
  - **For T46:** continue-as-new must carry the parked-wait count, or the next
    execution would reuse wait id `…-trigger-0`.

### ✅ Checkpoint J — orchestration without effects (before the first side effect)
- [x] A real trigger drives a real cycle through the worker, killed and resumed, with no effect wired
  (`tests/durability/test_worker_death.py`: SIGKILL while parked, a trigger delivered with
  no worker alive, and a fresh process resumes in ~10.2s; scored once, wait settled)
- [x] `tests/durability`, fitness, gates green (98%, stack_guard intact, 13/13 gates)
- [x] Human review (2026-09-27: "let's continue", taken as accepting the proposals below)
  (a) **Fixed.** A worker death *mid-scoring*: the retried `evaluate_cycle` found the
  claim unsettled and returned `outcome=None, refusal=None` as a success, with 0 scoring
  calls (verified first with a throwaway probe). The activity now uses
  `cycles.evaluate_to_settled`. The run's workflow is the cycle's only evaluator, one
  cycle at a time, so a claim it finds unsettled is its own dead attempt: it evaluates
  again and settles, or is refused again. `cycles.evaluate` keeps its meaning for
  `fanout.py`, whose racing threads rely on it until T45 retires them. Two tests: the
  retry finishes the cycle (scored once, settled), and a refused cycle is refused
  again, never resumed into an outcome.
  (b) **Fixed.** `operator stalled` now lists cycles still unsettled
  `UNSETTLED_AFTER` (10 min) after their claim, and exits 1 on them. That makes
  migration 0005's comment true. Every cycle is unsettled while it scores, so a fresh
  claim isn't reported (tested both ways).
  (c) **Logged, not fixed: pre-existing, outside Phase 8.** The `trigger_cycles` key is
  `(experiment_id, data_as_of, kind)`, with no tenant, while `experiments` and
  `experiment_runs` key on `(tenant, experiment_id)`. `exp-7` at two tenants would
  share one cycle, and the second tenant's trigger would be deduplicated against the
  first's. The fix is a fix-forward migration adding `tenant` to the key, plus the
  store and the claim. It needs its own task and a human decision on the migration.

- [x] **T39 — Idempotency keys never derive from Temporal identity** · layer 6 · *XS*
  - Acceptance: nothing in `agentstack.tools` imports `temporalio`.
  - Verify: `test_idempotency.py` gains the import assertion (rule 1, part of C36).
  - Files: `tests/fitness/test_idempotency.py`.
  - **Done.** Checked on the import graph (grimp, the engine behind `lint-imports`,
    already installed with it), so an indirect path counts too. There's a control
    assertion that `agentstack.runtime` *does* reach `temporalio`, so the test can't
    pass by building a graph without external packages. Mutation-checked: a planted
    import in `tools/spec.py` fails with the chain named.

- [x] **T40 — The turn as one activity** · layer 3 · *M*
  - Acceptance: `run_turn` activity runs the LangGraph graph (Postgres checkpointer,
    `durability="sync"`), mints the envelope inside, and passes ids in and out; its
    timeout is 120s, with a heartbeat. PRE_COMMIT registry writes go through the gateway from here.
  - Verify: `test_turn_graph` green; worker SIGKILLed mid-turn → resumes without
    re-calling the model (`tests/durability/test_process_death.py` re-pointed); retry,
    reset and redelivery of the activity → the registry write lands once (C36).
  - Files: `runtime/temporal/{activities,workflows}.py`, `tests/durability/{worker,test_process_death}.py`.
  - **Done, with the input decided at T40 (human, 2026-09-27): ids now, evidence later.**
    After a `propose` cycle the workflow takes a `draft` turn. `runtime/drafting.py`
    builds the instruction from the cycle's record (tenant, experiment, version,
    watermark) inside the activity, so no prompt enters history. Feeding the cohort's
    evidence (size, threshold, value at risk) is its own layer-5 task: it isn't stored
    when a cycle settles.
  - **Turn identity comes from the cycle, not from Temporal:**
    `{stage}:{experiment}:{watermark}:{kind}`. Every attempt and every later execution
    is the same LangGraph thread. `graph.finished_turn` makes a finished turn its own
    answer, since `advance` would start a finished thread over.
  - **Session work stays above the runtime.** `TurnHost` (runtime) is implemented by
    `wiring.ExperimentTurns` (interfaces): it resolves the session, mints the envelope
    inside the activity, and writes the transcript, as `handle()` does. Least privilege
    per stage: a `draft` turn's envelope carries only `experiments:write`.
  - The stage is the workflow's choice (position), passed in the intent. Exposure
    follows it, and approval tiers still gate commits. `contracts.DRAFT` is held equal
    to `tools.experiments.DRAFT_STAGE` by a test.
  - The heartbeat is a thread in a copy of the activity's context: 5s beats, 15s
    timeout. It's verified by a 20s turn that completes in one attempt, and
    mutation-checked: without beats that turn never completes.
  - 13 tests: `test_turn_activity.py` has 7 (drafted once and audited with
    `approval.policy:`; no instruction or envelope in history; transcript; a rerun, a
    redelivery after close and a **workflow reset** each leave one model call and one
    draft; the heartbeat). `test_worker_death.py` gains the mid-turn SIGKILL: the model
    answered, the gateway not yet called, and a fresh process finishes in ~16s without
    asking again. `test_turn_graph.py` gains the resume-at-`call_model` test.
  - **Found and fixed, pre-existing since T8a:** a turn resumed at `call_model` (its
    process died *during* the model call, the likeliest death) raised
    `KeyError: 'bundle'` on every retry, forever. The bundle was only ever built by
    `assemble` in the dying process. `call_model` now reassembles it and records the
    fingerprint it actually saw. The checkpoint shape is unchanged. The mutant heartbeat
    run is what surfaced it.
  - **Also fixed:** the `ModelEngine` protocol declared `asset` settable, and
    `OllamaEngine` never matched it. Nothing writes `asset`, so the protocol now asks
    for a readable one. A worker with no turn host fails a turn once, non-retryably
    (`NoTurnHost`), and doesn't spin.
  - `agentstack-worker` now builds the full stack with `OllamaEngine()` (qwen3:8b).
  - `test_process_death.py` is kept as it is, since it still proves the LangGraph
    checkpoint path directly, and the Temporal re-pointing is the new test beside it.

### ✅ Checkpoint K — first side effect from inside an activity
- [x] A PRE_COMMIT write from the turn activity commits once, audited with its rule
  (`test_turn_activity`: one `committed` audit record whose decision is
  `approval.policy:…`; also after SIGKILL, retry, redelivery and reset)
- [x] No envelope or prompt in the recorded history (spot check on the real history JSON:
  no instruction text, no `vault://` credential ref, no scope)
- [x] Human review (2026-09-27: "let's continue")

- [x] **T40b — Record the frozen cohort** · layers 5, 3, 10 · *S* (added 2026-09-27)
  - **Why it's here (human decision):** T41–T43 need facts about the frozen cohort that
    were never stored. The rollout payload needs its predicate (`targeting_model_version`,
    `risk_threshold`), and the Slack ask needs its size and value at risk (approval
    legibility). T40 deferred cohort *evidence for the model*; these are the action's
    own arguments and what the approver must see. Also found: nothing in production
    posts the first approval ask, since `post_approval` and `fire_reasks`'s `ask` are
    only ever called from tests. T41 has to fill that seam.
  - Acceptance: migration `0014` adds `frozen_cohorts` (insert-only, by trigger, like the
    registry history), written for a `propose` cycle by the evaluation activity.
  - **When it is written:** `cycles.evaluate_to_settled` gains a `before_settle` hook,
    called after layer 8 authorised the outcome and before the cycle settles. A refused
    proposal leaves no row, and a death between the two is finished by the rerun (same
    version, same row). The other order could lose the cohort for good, because a
    settled cycle is never evaluated again.
  - Verify: `tests/durability/test_frozen_cohorts.py`, 5 tests: a proposal records it;
    a refused proposal and an abstention don't; death between record and settle converges
    on one row; UPDATE and DELETE are refused with the constraint named.

- [x] **T41 — Approval wait and re-ask on timers; retire `deadlines.py`** · layer 3 · *M*
  - Acceptance: `park_wait` activity writes the wait row (fingerprint, summary,
    snapshot) and asks; the workflow re-asks on a timer, never expiring silently.
    `deadlines.py` is deleted once its tests pass against the timer loop.
  - Verify: time-skipping: 72 simulated hours unanswered → asked at every interval
    (C32); `test_stalled_waits` re-ask tests green; `park_wait` rerun → one wait row.
  - Files: `runtime/temporal/{workflows,activities}.py`, `runtime/deadlines.py` (deleted), `tests/durability/test_timers.py`.
  - **Done. The flow SPEC.md describes now runs end to end up to the answer:** propose →
    draft turn (PRE_COMMIT, commits) → **rollout turn** (told the action from the frozen
    cohort: 10% per SPEC, predicate in the payload) → the gateway stops it (ALWAYS), `act`
    parks the `human_approval` wait → **`ask_approval`** puts the question → the workflow
    waits on `REASK_EVERY` (24h) and asks again each time, never expiring.
  - **The wait is parked where it always was, by the turn's `act`,** not by a separate
    `park_wait`. What changed is that its id is now derived
    (`waits.approval_wait_id(run, fingerprint, snapshot)`), so a turn that parked it and
    died before its checkpoint parks the same wait again. A different snapshot gives a
    different wait: the question is about that world.
  - **The ask seam is filled for the first time.** Before this, nothing in production
    ever posted an approval (`post_approval` and `fire_reasks`'s `ask` were called only
    by tests). `Asker` (runtime) is implemented by `wiring.ChannelAsker`: it builds the
    real `ApprovalAsk` from the wait's summary and the frozen cohort
    (`estimated_customers = round(size × 10%)`, value at risk) and posts it.
    `agentstack-worker` uses `SlackNotifier`; tests use the recorder.
  - **Re-asks are recorded on the wait** (`reasks`, next `deadline`) by
    `WaitStore.record_asked`, which replaces `record_reask`. It only moves forward and
    only while pending, so a rerun counts once and an answer that lands mid-ask wins.
    `deadlines.py` is deleted. Its five tests in `test_stalled_waits` were rewritten in
    place against `ask_approval` (plus one for an ask with no summary), and the timer half
    is in `test_approval_wait` (72 simulated hours: asked 4 times, `reasks == 3`, still
    pending).
  - **Semantics made explicit:** while a run waits for a person, new triggers queue
    behind the answer. That's the existing Part 3 gate ("a run with an unsatisfied wait
    doesn't look at anything else"). Checkpoint J's kill test now opens with a
    `metric_movement` (refused, so the run parks on a trigger wait) to keep testing what
    it was about.
  - T40's tests now count *drafting* calls (the rollout turn is a second, legitimate
    model call) and wait until the run settles at the approval before tearing down.
  - `stack_guard` flagged 4 assertions fewer in `test_stalled_waits.py`. Instead of
    explaining it away, the rewritten tests gained real assertions: the question's
    content and size, visible duplicates, nothing posted on failure or after an answer.
    It's intact now.

- [x] **T42 — The Slack answer notifies the workflow** · layer 1 · *S*
  - Acceptance: `slack_callback.py` runs `ApprovalCoordinator.apply` as today, **then**
    `client.notify_answer` (a signal). The signal carries ids only.
  - Verify: `test_slack_inbound`, `test_approver_authorisation` green; an outsider's
    answer writes no approval, and the commit refuses (C39).
  - Files: `interfaces/slack_callback.py`, `runtime/temporal/client.py`, test.
  - **Done.** `wiring.answer` is the composition root for the Slack answer, as `deliver` is
    for triggers. It calls `slack_callback.accept` (signature, freshness, replay), then
    `coordinator.apply` (authorised against the run's tenant, recorded against the wait),
    then `client.notify_answer`, a signal carrying the wait id and nothing else.
    `slack_callback.py` itself is unchanged: it proves the request and holds no stack.
  - **Lost-signal backstop:** `ask_approval` now returns `AskResult(answered)`. If the
    answer was recorded but its signal never arrived, the next re-ask finds the wait
    satisfied and the workflow takes that as the answer. So a lost wake-up costs one
    interval, not the run, and nothing is posted again.
  - 4 tests in `test_slack_answer.py`, through signed Slack payloads: an approver's
    answer is recorded, then wakes the run; **an outsider's answer changes nothing** (no
    approval, the wait still pending, the run not woken: the first half of C39, the
    commit refusal is T43's); a refusal is recorded and wakes the run; a lost signal is
    found at the next ask.
  - Counting approvals means counting *human* ones: the draft's PRE_COMMIT grant is a
    policy row in the same table (migration 0006).

- [x] **T43 — The commit activity: snapshot at the act** · layer 3 · *M*
  - Acceptance: `commit(CommitIntent)` takes no snapshot argument; it reads the snapshot,
    mints the envelope and calls `gateway.execute`. `UnresolvedEffect` parks a
    reconcile wait. This is the irreversible act.
  - Verify: `test_state_snapshot` extended: the world moves after approval →
    `ApprovalStale`, one attempt, audited, no commit (C33); an unresolved rollout → one
    attempt, parked, reconciled, deduped (C34); `test_approve_resume_rollout`,
    `test_unresolved_effects` green.
  - Files: `runtime/temporal/{activities,workflows,contracts}.py`, `tests/fitness/test_state_snapshot.py`.
  - **Two decisions (human, 2026-09-28): "world only", and take the checkpointed proposal.**
    Layers 7 and 3, not only 3. The old snapshot mixed in the rollout turn's context
    fingerprint, which a commit with no turn can't recompute. Copying it from the wait
    instead would be E3: the check compares the snapshot with itself.
  - **The world, read at the act.** `SurfaceClient.state(resource)` says what the resource
    *is*, and `Gateway.observe` is the one path to it (sandboxed; outside containment it
    returns nothing, so the refusal stays in `execute`, audited). For the registry, that's
    the experiment's current version, variant and latest hypothesis. The version is derived
    from the scored data, so it covers `data_as_of`. **Status is left out on purpose:** the
    rollout itself takes the experiment live, and if that moved the snapshot, the retry
    that should deduplicate would read as stale. The lifecycle is the surface's precondition
    (`SurfaceRefused`). `snapshot.world_snapshot` has no context fingerprint. `act` uses it
    whenever the surface describes the resource. The reference API describes nothing and
    keeps the old snapshot, so refunds are unchanged.
  - **The act.** After the answer, the workflow runs `commit(CommitIntent)`: run, wait and
    cycle ids only. The activity reads the rollout turn's checkpointed proposals, prepares
    them again against the rollout stage's exposure, and commits only the one whose
    fingerprint equals the wait's (`NothingApproved` otherwise). It observes the world,
    mints the envelope and calls `gateway.execute`. It checks no yes/no itself: a "no",
    an outsider or a forged signal leaves no human approval, and the gateway refuses
    (`ApprovalRequired`), audited, one attempt. A world that can't be described is
    `WorldUnreadable`, non-retryable. Declared `gateway` in the interceptor.
  - **`UnresolvedEffect` parks a `reconcile` wait** (id derived from run and fingerprint).
    The run waits for the `answered` signal, which now means any satisfied wait, then acts
    again, and the ledger deduplicates.
  - 17 tests. `test_state_snapshot` +6 (Postgres registry through `observe`): a revised
    hypothesis moves the world, the rollout doesn't move its own, the API falls back,
    outside containment is nothing. `tests/durability/test_commit.py`, 7, on the
    time-skipping server: approved → committed once, audited with the human approval,
    and a direct rerun deduplicates (C36); **revised after the approval → `ApprovalStale`**,
    one audit record, no rollout (C33); a forged wake-up and a "no" → `ApprovalRequired`
    (C39); an unproposed action and an unreadable world refused; **lost answer → one
    attempt, parked, reconciled, deduplicated, surface called once** (C34).
  - **Mutation-checked:** with the activity passing `wait.state_snapshot` (E3's carried
    snapshot), the C33 test fails because the stale approval *commits*.
  - **Not done here, named:** no reconcile command. The test does what a reconciler would
    (finalize the claim, satisfy the wait, signal). A reconcile wait has no deadline, and
    `operator stalled` doesn't list it; `ledger.unresolved_keys()` and the `unresolved`
    audit record are the operator's view today. Belongs with T50. The workflow change
    isn't behind `workflow.patched()`: nothing is live, and T47 brings the replay guard.
  - **Shared test database, again.** Sessions in the main checkout kept running
    `check_task.sh` and dropping `agentstack_app_test` mid-run: dozens of spurious
    failures (`AdminShutdown`, rows vanishing). Verified instead against a throwaway
    Postgres on 5434, as in T23: **675 passed**. `stack_guard` intact, `checkpoint_guard`
    safe, changed-line coverage 94%. A per-invocation test database is still owed.

- [ ] **T44 — Death while parked, end to end** · layer 3 · *S*
  - Acceptance: the SPEC.md criterion 23 path runs on Temporal.
  - Verify: `tests/durability/test_approve_after_death.py` re-pointed: SIGKILL while
    parked, fresh worker, real Slack path; prepare, ask and commit each happen once;
    resume ≤15s (C31).
  - Files: `tests/durability/{park_worker,test_approve_after_death}.py`.

### ✅ Checkpoint L — the approval boundary under Temporal
- [ ] Trigger → score → draft → Slack → approve → rollout, killed while parked, still correct
- [ ] Stale approval refused at the act; unresolved effect not retried blind
- [ ] `/stack-audit` on the diff so far
- [ ] Human review

- [ ] **T45 — Bounded fan-out; retire `fanout.py`** · layer 3 · *S*
  - Acceptance: worker `max_concurrent_activities` bounds evaluations; `fanout.py` is
    deleted once its tests pass.
  - Verify: `tests/durability/test_concurrency.py` re-pointed: 100 triggers, peak ≤
    the limit, each commits once (C42).
  - Files: `runtime/temporal/worker.py`, `runtime/fanout.py` (deleted), `tests/durability/test_concurrency.py`.

- [ ] **T46 — Continue-as-new** · layer 3 · *S*
  - Acceptance: every 100 cycles, carrying `run_id` and the current wait id.
  - Verify: time-skipping: 250 cycles → same `run_id`, same pending wait, one audit trail (C44).
  - Files: `runtime/temporal/workflows.py`, `tests/durability/test_timers.py`.

- [x] **T47 — Replay guard** · layer 3 · *M*
  - Acceptance: `scripts/replay_guard.py` replays `tests/fixtures/histories/*` (recorded
    by the durability tests) against this code; it runs in `check_full.sh`.
  - Verify: an unguarded change to `ExperimentWorkflow` fails the guard, and the same
    change behind `workflow.patched()` passes (C41); `checkpoint_guard` unchanged.
  - Files: `scripts/replay_guard.py`, `scripts/check_full.sh`, `tests/fixtures/histories/`, `tests/fitness/test_replay_guard.py`.
  - **Done. Four histories, recorded on purpose.** `tests/durability/test_recorded_histories.py`
    drives four real paths through the production worker on the time-skipping server,
    checks each got where it was meant to, and hands the run to
    `temporal_support.keep_history`: `trigger_cycles` (a cycle abstains, the trigger
    wait goes overdue on its timer, a `metric_movement` is refused by layer 8, parked
    again); `approved_commit` (propose → draft → rollout turn → park → ask → a day
    unanswered → re-ask → approved through signed Slack → committed); `refused_at_the_act`
    (a wake-up nobody authorised → `ApprovalRequired`); `unresolved_reconciled` (answer
    lost → reconcile wait → woken → deduplicated).
  - **Re-recording is one command:** `DATABASE_URL=... uv run python scripts/replay_guard.py --record`.
    It reruns that module with `AGENTSTACK_RECORD_HISTORIES=1`, rewrites the four files,
    then replays them. Without the variable the suite still runs the scenarios and
    replays each fresh history, so a scenario that stops replaying is found by the suite,
    but nothing is written: a fixture changes only when someone means it to. **T45/T46
    re-record after they merge** (their workflow changes are not behind `patched()`,
    and nothing is live yet). Once a run is live, add histories beside the old ones
    instead of overwriting (deleting one is ask-first).
  - **What's in the files:** ids and verdicts only (checked by hand: no envelope,
    `credential_ref`/`vault://`, prompt, instruction or scope). Failed activities' stack
    traces are emptied on recording: they named the recording machine's paths, and
    replay never reads them. The failure's type and message are kept. T48 enforces this.
  - **C41, proved in `tests/fitness/test_replay_guard.py` (7 tests):** every recorded
    path exists; the real code replays all four; a mutant deploy
    (`tests/fitness/replay_mutants.py`, an extra `ensure_run` before each evaluation)
    fails **every** history with `TMPRL1100` nondeterminism; the same step behind
    `workflow.patched()` passes; `main` exits 1 and names the history and the fix; no
    histories is exit 2, not a pass. Also checked by hand against the real workflow: a
    `workflow.sleep` inserted in `run` failed all four; behind `patched()` all four
    passed. Reverted.
  - **Where it runs: `check_task.sh`, next to `checkpoint_guard`,** so `check_full.sh`
    runs it through `check_task` (a comment there says so). Replaying four histories
    takes about half a second, and the four scenarios about 4s in the suite. That's
    well inside the 90s budget. `checkpoint_guard` is unchanged.
  - 686 passed (675 + 11) against a throwaway Postgres on 5437. `stack_guard` intact,
    `checkpoint_guard` safe. README fitness count 41 → 42 (the README test enforces it).
  - **Not done, named:** the fixtures aren't byte-stable across recordings (fresh uuids
    and timestamps each time), so every re-record is a full diff. `data/manifest.json`
    isn't tracked on this branch (`.gitignore` excludes `data/`), so
    `test_data_snapshot` fails in a fresh worktree until it's copied in. That's
    pre-existing, and I didn't fix it here.

- [ ] **T48 — Nothing secret in history** · layer 3 · *S*
  - Acceptance: a test decodes every payload in every recorded history.
  - Verify: no envelope, `credential_ref`, prompt or evidence field anywhere (C40).
  - Files: `tests/fitness/test_temporal_boundaries.py`.

- [ ] **T49 — Traces across workflow and activities** · layer 9 · *S*
  - Acceptance: every activity's spans carry our `run_id` and `session_id`; Temporal
    history is never read as audit.
  - Verify: `test_trace_completeness`, `test_audit_separate_from_traces` green through
    the worker.
  - Files: `runtime/temporal/activities.py`, `tests/fitness/test_trace_completeness.py`.

- [ ] **T50 — `operator status` joins position and record** · layers 3, 1 · *S*
  - Acceptance: status shows the workflow's position (Temporal) beside the run's record
    (Postgres); `stalled` still reads only `waits`.
  - Verify: `test_stalled_waits` operator tests green; a new status test for a parked run.
    Status flags a pending activity past 10 attempts, with its last failure, because
    the retry policy has no cap by design (decided at Checkpoint I).
  - Files: `runtime/operator.py`, `interfaces/operator_cli.py`, test.

- [ ] **T51 — Ledger and bar** · docs · *S*
  - `STACK.md` rows 3 and 10 (Temporal owns position, Postgres the record); `CONSTRAINTS.md`
    additions only: floor bullets "No identity envelope, credential or prompt in
    workflow history", "No idempotency key derived from Temporal identity", "No gateway
    refusal retried"; enforced rows for the replay guard and `test_temporal_boundaries`;
    fitness count ratchet; README counts.

### ✅ Checkpoint M — Phase 8 complete
- [ ] Spec criteria C29–C45 met; SPEC.md criteria 1–28 still hold
- [ ] `check_full.sh` green, including `replay_guard` and `checkpoint_guard`
- [ ] `/stack-audit` run and its findings addressed
- [ ] Human review
