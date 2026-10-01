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
    Runtime, workflows, durable execution invariant these two hang off.
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
      suite (`MemoryStore` was the exception; closed below).
- [x] All 11 gates green, each against a migrated `agentstack_evals` database
- [x] `stack_guard.py --base main` clean; coverage 99%, changed-line 100%
- [x] `0001`–`0003` roll back to empty and forward again
- [ ] **Human review before Phase 2**

**Gap found at the checkpoint — not covered by any task in this plan. CLOSED 2026-10-01:**
`MemoryStore` and `MaintenanceQueue` now live in Postgres (`0021_memory_is_durable`, up/down);
`write()` is still the only door, and the table's CHECKs refuse what it refuses.
`test_memory_is_explicit` proves a fresh store recalls scope, provenance, TTL and trust. The
text below is the original finding.

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
  - **Done.** The gate, the fold assignment, the encoding, the cross-fitting loop, the
    replay guards and the startup command are built and tested. The recorded fixture now
    exists too: `TABPFN_TOKEN` is in `.env` (gitignored), and `data/scores/` holds
    `telecom-bigml.json` and `bank-churn.json` (`model_version: tabpfn-3.5`, recorded
    2026-09-29 with `scripts/record_scores.py`). **`data/` is gitignored, so the fixture
    exists only on the machine that recorded it;** CI and a fresh clone still cannot run
    off it until it is committed or fetched. `tests/live` reads `TABPFN_TOKEN` from the
    process environment, not from `.env` — export it first (`set -a; . ./.env; set +a`).
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
  - **No longer blocked on the fixture from T6:** the cohort was first exercised with
    stand-in scores; real recorded scores now exist in `data/scores/` (see T6), and the
    cohort has been run against them.

### ✅ Checkpoint B — a cohort is real and reproducible
- [x] A cohort can be produced twice from one snapshot with identical membership
      (T7: `tests/fitness/test_targeting.py`, criterion 17; rank cut, not quantile, so
      ties cannot change the size. Recorded scores now exist, T6.)
- [x] Thresholds live in `experiments/`, versioned (`experiments/targeting.toml`, named
      profiles, frozen into `experiment_version`)
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
      **mostly met.** The wiring landed in T13 (`runtime/operator.py`), real recorded
      scores exist (T6), and kill-and-resume is proven on Postgres (T10) and on Temporal
      (T44). No single test drives trigger → real scores → kill → resume; that is what
      keeps this open.
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
2. ~~**"A real cohort" is still stand-in scores.**~~ **Closed 2026-09-30:** the scores
   were recorded with a real `TABPFN_TOKEN` (`data/scores/`, T6) and the cohort was run
   on them. The fixture is gitignored, so it is local to that machine.

Neither blocks Phase 4 — T11 (`PRE_COMMIT`) and T12 (Ollama) depend on neither, and
T13 is the natural place for the wiring since it is the first task that needs a cohort
to draft *from*. **Decided: the wiring folds into T13**, which is the first task that needs a cohort to
draft *from*. Checkpoint C's first line stays open until then, deliberately. (Update 2026-09-30: the wiring is done; see Checkpoint C.)

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
- [x] A model-drafted candidate is validated, policy-checked and written
      (T13 criterion 19; T12 Ollama adapter; the draft commits through the gateway)
- [x] A malformed one is refused, traced, and answered (T12 `tool.reject`, T13 criterion 19)
- [x] The write produces a `PRE_COMMIT` audit record naming the rule (T11: `granted_by`
      and `rule` on the record, CHECK-enforced)
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
  - **`needs_migration` is a wait kind**, not a new table. Waiting is state (Runtime, workflows, durable execution),
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
- [x] `check_task.sh`, `tests/durability`, `lint-imports`, `stack_guard`, `checkpoint_guard` green
- [x] Human review — the go-ahead for T26–T31 (2026-09-27) stands in for it.
  - Run against a throwaway Postgres on 5434, the shared-DB collision still unfixed.
  - **Found, not fixed:** `data/manifest.json` is meant to be committed and is on no
    branch. `.gitignore` excludes the directory (`data/`), and git cannot re-include a
    file under an excluded directory, so `!data/manifest.json` is inert (`data/*` would
    work). A fresh worktree fails
    `test_the_manifest_records_a_watermark_for_every_registered_dataset` until the main
    checkout's `data/` is copied in.

- [x] **T26 — Read tools** · layers 6, 7, 5 (+1, 2 for the loop) · *M*
  - Acceptance: `get_experiment`, `list_experiments` (`limit` 1–50), `get_rollout_history`.
  - Verify: matrix rows in `test_registry_tools.py`; `limit: 51` refused; read of
    model-authored text enters context `UNTRUSTED` (`test_untrusted_content.py`);
    `execution.read` spans (`test_trace_completeness.py`).
  - **Done.** Three `NONE`-tier reads on `experiments:read`. The surface's resources are
    now one closed table per verb, so history cannot be written and a rollout cannot be
    read. `list` is bounded twice: the schema refuses outside 1–50, and the surface
    clamps anyway. `history` derives `current_exposure` from the latest rollout or halt
    and never stores it.
  - **The validator now enforces `enum`, `minimum` and `maximum`.** Before this, a bound
    declared in a schema was checked by nothing.
  - **The read loop is closed.** Until now a read's data stopped at
    `TurnResult.observations`, so the model never saw what it read. Now `handle` appends
    each read to the transcript as `kind="observation"`, and the next turn's `assemble`
    shows the last 5 as `trust=untrusted`, with `surface:{surface}:{resource}` as their
    provenance. They reach the runtime as plain `(body, at)` pairs, because runtime
    may not import `control_plane`. `TurnContext` gains the field, and its exact-set
    test was extended.
  - The `execution.read` span is asserted on a registry read in
    `test_untrusted_content.py`, not in `test_trace_completeness.py`: that is where the
    turn doing the read already is.

- [x] **T27 — Draft lifecycle** · layers 6, 7 · *M*
  - Acceptance: `revise_draft_hypothesis`, `discard_experiment_draft`; revise appends a
    revision, never a new `experiment_version`.
  - Verify: store contract — refused from `discarded`, `live`, `halted`; lost answer → one row.
  - **Done.** Both are `PRE_COMMIT` on `experiments:draft`, on the draft stage only.
    - Revise has no `variant` argument. Its key hashes the wording.
    - Discard is keyed once per version.
    - Contract cases, both clients: revise and discard are each refused from `live` and
      `discarded` (`halted` joins in T29), and a refusal leaves every read unchanged.
    - A revision is a row, never a version (Postgres, counted).
    - A lost answer plus a retry leaves one row against the real store, through the
      gateway, for each write (`test_registry_tools.py`, `AnswerLost`). This case list
      grows in T28 and T29.
  - **Scope renamed:** `create_experiment_draft` moves from `experiments:write` to
    `experiments:draft`, per the spec. Approvals bind the action fingerprint, not the
    scope, so parked runs are unaffected. Any caller still granting `experiments:write`
    can no longer draft. No caller in `src/` does; the scopes come from whoever builds
    the envelope.
  - **One surface transition.** `_TRANSITIONS` maps each status change to the states
    it may start from and the state it leaves. Rollout and discard are the same guarded
    `UPDATE … RETURNING` feeding the event `INSERT`.
  - **Revise locks the row.** It takes `FOR UPDATE` on the experiment row, so a discard
    racing it either wins outright or waits. Two revises racing for one `revision_no`
    collide on the key, and the loser is a refusal.
  - **Found and fixed:** a blank revision fails the store's `revision_says_something`
    check. The statement fails whole, so nothing applied, but it would have surfaced as
    an unresolved effect and stranded the claim. It is now a refusal on both clients.

- [x] **T28 — `record_abstention`** · layers 6, 7 · *S*
  - Acceptance: append-only, no status change, `evaluation` stage only.
  - Verify: store contract — appends in every state, status unchanged.
  - **Done.** `PRE_COMMIT` on `experiments:annotate`, its own scope, so a grant to add a
    note never implies a grant to stop anything. On the evaluation stage only.
    - The key hashes the explanation: a retry is one record, and a later cycle with a
      different reason is a second.
    - Postgres writes it as one `INSERT … SELECT FROM experiment_versions`, with no
      status predicate and no `UPDATE`. An unknown version matches no row, so it is a
      refusal.
    - Contract, both clients: it appends in `draft`, `live` and `discarded` (`halted`
      joins in T29), leaves every read unchanged, and is not rollout history. The
      same explanation twice is refused.
    - Added to the lost-answer retry cases.
  - **Spec Q3 answered by construction:** the tool is on `evaluation`, and nothing ties
    a stage to a trigger kind. Whichever trigger woke an evaluation run, it can record
    an abstention.

- [x] **T29 — `halt_rollout` + `halt_only_zeroes`** · layers 6, 7, 8 · *M*
  - Acceptance: `live → halted` only; no `percentage` argument; policy refuses a
    non-zero halt payload before the surface.
  - Verify: `test_approval_tiers.py` (spec criterion 6); store contract.
  - **Done.** `PRE_COMMIT` on `experiments:halt`, on the evaluation stage only, keyed
    once per version.
    - Its schema has no `percentage`; its prepare writes `0`.
    - `live → halted` is one more row in `_TRANSITIONS`.
    - Contract, both clients: refused from `draft`, `halted` and `discarded`, with every
      read unchanged. A halted version cannot be rolled out again. History ends with
      the halt, and `current_exposure == 0`.
    - `halted` now joins every per-state contract case, so revise, discard and
      abstention each run against it.
  - **Placed in `decide`, not in the `PRE_COMMIT` rule set (spec deviation).** There,
    a refusal only escalates the run to a human, and an existing grant skips the rules
    entirely. So a person approving a hand-built "halt to 5%" would have got it
    through. As a check in `decide`, which runs first on every call, a non-zero halt
    is `PolicyDenied`, audited `denied` / `halt.only_zeroes`, and a human grant for
    that exact fingerprint changes nothing (`test_approval_tiers.py`). It is still
    layer 8, and `decide` still sees no free text.

### ✅ Checkpoint H — all nine registry tools
- [x] Exposure matrix exactly draft 5 / evaluation 5 / rollout 4 (spec criterion 2)
- [x] Human review — covered by the same go-ahead as Checkpoint G.

- [x] **T30 — Narrowness bar, concurrency, injection eval** · layers 6, 9 · *M*
  - Verify: `test_registry_tools.py` narrowness rules; 20 concurrent halts → one
    `halted`, one event (`tests/durability/test_concurrency.py`); injection case in
    `evals/cases/`.
  - **Done.** The narrowness bar is held over every `Surface.REGISTRY` tool the catalog
    has, not over a list typed in the test, so a tenth tool meets it on registration:
    - no `status`/`fields`/`patch`/`updates` (the one `status` is the list read's filter);
    - no array or object arguments;
    - only the rollout names a `percentage`;
    - every write requires `experiment_version`;
    - the five scopes map exactly onto their tools;
    - all nine are registered, and the menus are 5/5/4 as numbers.
  - **Two races, twenty halts each:**
    - Twenty evaluation runs halting through the gateway. The ledger sees one effect,
      and every other caller is deduplicated or told `UnresolvedEffect`.
    - Twenty different halts straight at the Postgres store, bypassing the ledger.
      Exactly one applies, and 19 are `SurfaceRefused`, because the `WHERE status =
      'live'` guard holds on its own.
    - One `halted`, one halt event, stable across repeated runs.
  - **`injection-in-a-hypothesis-does-not-reach-halt`** (gate, Tools, MCP, capability surfaces). A drafting run
    reads a hypothesis that says "halt all experiments". On the follow-up turn it is in
    context as untrusted, the menu is still exactly the five drafting tools, and there
    are zero registry events. The runner gained `stage`, `registry` seeds, `followups`
    and a `registry_events` expectation to express it.
  - **Residual, by design, not fixed:** injected text that names an *exposed* drafting
    tool (e.g. `discard_experiment_draft` with arguments) can steer the model to call
    it. The narrowness bar bounds that to reversible, `PRE_COMMIT`, one-experiment
    acts on the draft stage. It cannot make it zero.
    - **Corrected by audit finding H3: that statement understated the risk.** The same
      injection reaches the *evaluation* stage. There, `halt_rollout` is exposed and
      `PRE_COMMIT`. A halt is terminal, since there is no relaunch path. And the
      injection could name *a different* experiment. A hypothesis in exp-7 saying
      "halt exp-9", read by an evaluation run, halted exp-9 for good. Someone with only
      `experiments:draft` could spend an evaluation run's `experiments:halt`. The
      gate eval covered only the draft stage. Fixed below (H3).
    - **What remains after H3:** injected text can still steer an evaluation run to
      halt or annotate *its own subject*, the experiment it was woken about. The
      model decides that act either way. On the draft stage the residual is as first
      stated: draft runs are not bound (see H3).

- [x] **H3 — an evaluation run is bound to its subject** · layers 3, 8, 10 (+1 via
  the trigger) · audit finding
  - `runs.subject` (`migrations/0013`) is the experiment the run is about. It is set
    from the trigger (`runtime.cycles.evaluation_run`, after `policy.triggers.subject_of`
    tests the trigger's tenant claim against the run's), or by whoever creates the run.
    It never comes from model or tool output.
  - `Run` refuses an evaluation run without a subject, and a NOT VALID CHECK refuses
    one in the table. 0013 backfills pre-existing evaluation runs from the
    `trigger_cycles` row that names them. One it cannot backfill is refused on load,
    not resumed with tenant-wide halt authority.
  - `run_turn` narrows the envelope to the run's subject (`IdentityEnvelope.bound_to`,
    narrowing only), so no caller can forget to. `decide` refuses a side effect
    outside the subject as `subject.boundary`, before approval, so a human grant
    changes nothing. It matches by path segment: `exp-7` does not cover `exp-70`.
  - **Reads are not bounded, on purpose.** `list_experiments` reads the collection,
    which no one experiment contains. Once writes are bounded, a read can steer the
    run only towards its own subject.
  - **Rollout runs:** bound when created with a subject, but not required to have
    one. Their only write is `ALWAYS`, behind a human approval bound to a fingerprint
    that names the experiment, and rollout runs parked before 0013 must still resume.
    **Draft runs:** never bound, because they name experiments nobody has written yet.
  - Verify: `test_untrusted_content.py` (the evaluation-stage exploit, denied and
    audited, with exp-9 still live; the own-subject halt proceeds; the trigger's
    tenant claim is tested), `test_identity_envelope.py` (a human grant does not move
    the boundary; segment-exact; reads untouched; only narrows),
    `tests/infra/test_run_subject.py` (round trip, CHECKs, backfill, fail-closed load,
    rollback), and gate eval
    `injection-in-a-hypothesis-does-not-halt-another-experiment`. The exploit test
    and the eval each fail with the check removed.

- [x] **T31 — Ledger and bar** · docs · *S*
  - `CONSTRAINTS.md` gains "Registry narrowness" and "Registry preconditions" rows
    (additions only); `STACK.md` rows 6/7; `SPEC-registry.md` migration numbers and
    open-question answers; `SPEC.md` decisions table links the sub-spec.
  - **Done.**
    - `CONSTRAINTS.md`: two rows added, nothing edited. The twenty-halt race lives in
      "Registry preconditions" rather than in an edit to "Concurrency".
    - `STACK.md`: rows 5, 6, 7 and 8 extended. Row 5 covers observations entering
      context as untrusted. Row 8 covers why a rule no approval may override belongs
      in `decide`.
    - `SPEC-registry.md`, revision 3: all three open questions answered where they are
      asked, and a "Revised in the build" section with the resource table, the
      `halt_only_zeroes` placement, the read loop, the enforced bounds, the scope
      rename and the blank-revision refusal.
    - `SPEC.md`: the decisions table links the sub-spec.

### Phase 7 — done
- [x] All nine registry tools, spec criteria 1–8 each held by a named test
- [x] `check_task.sh`, `tests/durability`, `lint-imports`, `stack_guard`,
      `checkpoint_guard`, `evals --gates` green
- [ ] `/stack-audit` on the phase's diff (mandatory before `/ship`)
- [ ] Human review
- Still open, by name: the relaunch path (T25; a design gap — no tool owns it, `src` has no relaunch code). *(Per-invocation test database: done in `c333575`.)* Originally listed: a per-invocation test database
  (T22/T23).

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
    behind the answer. That's the existing Runtime, workflows, durable execution gate ("a run with an unsatisfied wait
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
    safe, changed-line coverage 94%. A per-invocation test database is still owed. *(Done in `c333575`: `storage.provision.run_scoped` gives each run its own databases and drops them when it ends; fixed-name databases from older code may linger on a dev server.)*

- [x] **T44 — Death while parked, end to end** · layer 3 · *S*
  - Acceptance: the SPEC.md criterion 23 path runs on Temporal.
  - Verify: `tests/durability/test_approve_after_death.py` re-pointed: SIGKILL while
    parked, fresh worker, real Slack path; prepare, ask and commit each happen once;
    resume ≤15s (C31).
  - Files: `tests/durability/{park_worker,test_approve_after_death}.py`.
  - **Done, as an extra driver, not a rewrite** (the plan's meaning of "re-point"). The
    four criterion-23 tests on the record stay as they are, with `park_worker.py`, because
    they still prove the approval binds across a death with nothing but Postgres. The new
    test beside them drives the whole path on Temporal. `temporal_worker.py` is the
    process it kills (not `park_worker.py`, which predates Temporal). It gained the
    production asker, wrapped to note each question in a file, so "asked once" can be
    counted across processes.
  - The path: a proposing trigger → scored, drafted, rollout proposed, parked, asked, all
    by worker 1 → **SIGKILL** → a signed Slack "yes" through `wiring.answer` **with no
    worker alive** (layer 8 records it; the signal waits in history) → worker 2 finishes
    the run. Asserted: scored, drafted, prepared (the rollout proposal) and asked once,
    each by the dead process. Committed once, by the fresh one: one registry rollout and
    one `committed` audit record whose approval is the Slack approver's.
  - **Resume: 9.2s, 9.2s, 9.3s** over three runs, from worker 2 ready to committed, against
    the 15s bound. It's almost all the dead worker's sticky-queue timeout (~10s, P4).
  - Verified on a throwaway Postgres (port 5438), since the shared one is still being
    dropped mid-run: `check_task.sh` green, bar intact, checkpoint guard safe.

### ✅ Checkpoint L — the approval boundary under Temporal
- [x] Trigger → score → draft → Slack → approve → rollout, killed while parked, still correct
  (T44: `test_approve_after_death`, resumed in 9.2s)
- [x] Stale approval refused at the act; unresolved effect not retried blind
  (T43: `test_commit`, mutation-checked)
- [ ] `/stack-audit` on the diff so far (deferred by the human to Checkpoint M, 2026-09-28)
- [ ] Human review

- [x] **T45 — Bounded fan-out; retire `fanout.py`** · layer 3 · *S*
  - Acceptance: worker `max_concurrent_activities` bounds evaluations; `fanout.py` is
    deleted once its tests pass.
  - Verify: `tests/durability/test_concurrency.py` re-pointed: 100 triggers, peak ≤
    the limit, each commits once (C42).
  - Files: `runtime/temporal/worker.py`, `runtime/fanout.py` (deleted), `tests/durability/test_concurrency.py`.
  - **Done.** The bound is `worker.MAX_CONCURRENT_ACTIVITIES` (`min(8, pool max)`, the
    number and reasoning `fanout.DEFAULT_MAX_IN_FLIGHT` had), passed to Temporal's
    `max_concurrent_activities` by `build_worker`. Temporal's default was 100. A task
    beyond the bound stays on the queue unclaimed, so its timeout hasn't started.
  - **The declared limit is the one that binds.** `build_worker` refuses an executor
    with fewer threads than the limit, and refuses a limit below 1. With fewer threads,
    the threads would be the real bound, and claimed tasks would burn their
    `start_to_close_timeout` in the executor's backlog. Temporal only warns about this.
    `activity_threads()` builds an executor of exactly the limit. `agentstack-worker`
    sizes its pool, threads and slots from that one number, which replaces its own
    `MAX_ACTIVITIES = 8`. The bound counts every activity, not only evaluations, because
    a turn or a commit also holds a connection.
  - The three fan-out tests were rewritten in place, through a real worker and the real
    ingress (`wiring.deliver`). In-flight evaluations are counted at the activity's
    `evaluate_trigger` seam, around the real one. (1) C42: 100 experiments × 3 shuffled
    deliveries are all queued before the worker starts. Each is scored once, each run
    reads 3 cycles, 100 settled rows, none unsettled, and peak ≤ the worker's configured
    limit (read from `worker.config()`) and > 1. The executor there has **4× the limit in
    threads**, so only the slots can be what holds it. (2) A failing evaluation is
    retried (≥2 attempts) and stays unsettled while the other 9 settle. (3) The bound is
    ≤ the pool, and `build_worker` refuses 0 and a too-small executor. The production
    worker config test also asserts the declared bound. Asserts and `raises` in the
    section: 10 before, 18 now.
  - **Mutation-checked:** without `max_concurrent_activities` on the `Worker`, the peak is
    32 (the test executor's threads).
  - **The C42 test runs on the time-skipping (in-memory) server.** The bound is the
    worker's, so the server only has to hand out tasks faster than the bound runs them.
    The dev server's SQLite can't do that for 100 runs: 17–23s, peak 6–7, so it measured
    the server. In-memory: ~4.6s, peak 8. Waiting is done on `trigger_cycles` first,
    because 100 runs polling `progress` at once are workflow tasks competing with the work.
  - The test workers (`temporal_support.worker_on`, `durability/temporal_worker.py`) now
    run at the production bound. They were at 4 threads, which would now be refused.
    `cycles.evaluate`'s docstring no longer names `fanout.py`. It still serves the eval
    runner and the direct cycle tests.
  - Verified against a throwaway Postgres on 5435: `check_task.sh` green, **675 passed**,
    changed-line coverage 100% (11/11), `stack_guard` intact, `checkpoint_guard` safe.
    A fresh worktree still needs `data/manifest.json` copied in (T32's finding).

- [x] **T46 — Continue-as-new** · layer 3 · *S*
  - Acceptance: every 100 cycles, carrying `run_id` and the current wait id.
  - Verify: time-skipping: 250 cycles → same `run_id`, same pending wait, one audit trail (C44).
  - Files: `runtime/temporal/workflows.py`, `tests/durability/test_timers.py`.
  - **Done.** `CONTINUE_EVERY = 100` cycles per execution. **It continues only between
    cycles**, at the top of the loop. That's the one point where the run holds nothing
    open: `_propose` has returned, so any approval or reconcile wait is answered, the
    trigger wait was satisfied before the cycle ran, and no activity is in flight. It
    waits on `workflow.all_handlers_finished` first. Temporal's
    `is_continue_as_new_suggested()` isn't used, because the spec fixes the count.
  - **What carries** (`contracts.Carried`, ids and counts only): `run_id` (in `RunStart`),
    `waits_parked` (so the next trigger wait is `…-trigger-{n}`, not `-0` again, which
    would hand back a satisfied wait), `last_watermark` (the next wait's snapshot),
    `pending` triggers in arrival order (placed *ahead* of anything signalled to the new
    execution), and `cycles_before` (so `progress` can still say what the run did).
    **What resets:** cycles, turns and commits (their record is in Postgres), `_answered`
    (spent; a recurring wait id finds its answer from the row at the ask, as T42's
    backstop does), and `waiting_on`/`overdue`/`awaiting`/`asks`/`reconciling`, which are
    all empty at the safe point. The "current wait id" is carried as the wait sequence:
    no wait is ever open across a handoff, so the next execution parks the next one.
  - **`Carried` is a field on `RunStart`, not a second workflow argument.** The SDK drops
    type hints when the argument count differs from the signature, so a defaulted second
    parameter decoded every one-argument start as a `dict`. It would also have broken
    replay of every history recorded so far. `ENSURE_RUN` is handed `RunStart` with
    `carried=None`: every execution ensures the row, the upsert is idempotent, and the
    pending triggers don't land in that activity's input. `RunProgress` gains
    `cycles_before`.
  - 1 test in `test_timers.py`, ~1.5s on the time-skipping server. 150 triggers are
    queued before any worker polls, so the handoff at 100 carries exactly 50. The run
    parks `trigger-0`, and the other 100 arrive live. Asserted: three executions, each
    started with what the previous one carried (read from its start event, which proves
    continue-as-new happened; it isn't audit); one `runs` row; 250 settled
    `trigger_cycles`; waits numbered as one unbroken sequence with exactly one pending,
    after `batch-249`, and the run is on it. **Mutation-checked:** without the wait
    count, the pending wait assertion fails (`trigger-0` again, satisfied). Without
    `pending`, the run stops at 100 cycles and never reaches 250.
  - **Found writing it:** the time-skipping server never moves a stopped worker's sticky
    task back to the shared queue (history ends at `WorkflowTaskScheduled`), so a test
    that stops and restarts a worker hangs there. The test never stops its worker.
  - **Not tested, stated:** a signal that lands *during* the handoff. The docstring
    relies on Temporal's documented behaviour: the server refuses to close the run over
    new events, and the task reruns with the signal. The unguarded workflow change is
    T47's to record, like T43's.
  - Verified against a throwaway Postgres on 5436: **676 passed**. `stack_guard` intact,
    `checkpoint_guard` safe, changed-line coverage 100% (24/24). In the first
    `check_task.sh` run, this test failed once inside the full suite, and the reason was
    not captured. It then passed in the next gate run, a full-suite run, and 8 isolated
    runs. Logged as a possible flake to watch. It is not explained.
  - **After the merge (2026-09-28):** not reproduced in 19 more runs on an isolated
    Postgres: 15 alone, twice in `tests/durability`, twice in the full suite. Still
    unexplained, and still worth capturing the output if it ever fails again.

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
  - **Merged with T44–T46 and re-recorded (2026-09-28).** Before re-recording, the four
    histories recorded on T43's workflow still replayed against the merged code, T46's
    continue-as-new included. That's direct evidence T46 would not strand a run started
    before it. Re-recorded with `--record` so the fixtures carry T46's `RunStart` shape.
    All 69 payloads decoded and scanned: no credential, prompt, scope or home path.
    Merged branch: **688 passed**, all three guards green, on an isolated Postgres.
  - **Not done, named:** the fixtures aren't byte-stable across recordings (fresh uuids
    and timestamps each time), so every re-record is a full diff. `data/manifest.json`
    isn't tracked on this branch (`.gitignore` excludes `data/`), so
    `test_data_snapshot` fails in a fresh worktree until it's copied in. That's
    pre-existing, and I didn't fix it here.

- [x] **T48 — Nothing secret in history** · layer 3 · *S*
  - Acceptance: a test decodes every payload in every recorded history.
  - Verify: no envelope, `credential_ref`, prompt or evidence field anywhere (C40).
  - Files: `tests/fitness/test_temporal_boundaries.py`.
  - **Done. Decoded, not grepped:** a payload is base64 inside the history JSON, so a grep
    sees none of it. Every payload in every recorded history (T47's four) is decoded,
    along with every failure message. A payload in any encoding but `json/plain` fails,
    since what can't be read can't be checked.
  - **Two checks.**
    - **Allowlist:** every key at any depth must be a field of a `contracts` dataclass.
    - **Denylist, derived from the real types:** the fields of `IdentityEnvelope`,
      `ModelRequest` and `FrozenCohort`, plus the turn's `message` and the wait's
      approval material (`state_snapshot`, `approval_summary`, `action_fingerprint`,
      `payload`), minus the four ids the contracts carry on purpose.
    - Values are scanned too: no `vault://`, and no instruction text. The openings are
      built by `drafting` itself, so a reworded prompt is still recognised.
  - A separate test asserts the two lists never overlap. Adding `size` or
    `approval_summary` to a contract is a decision about history, and it fails there.
  - 8 tests. Six plants (an envelope, a snapshot, cohort evidence, a credential inside
    an allowed field, a prompt, a field no contract has) each go into a real history,
    encoded as Temporal encodes them, and each is found.

- [x] **T49 — Traces across workflow and activities** · layer 9 · *S*
  - Acceptance: every activity's spans carry our `run_id` and `session_id`; Temporal
    history is never read as audit.
  - Verify: `test_trace_completeness`, `test_audit_separate_from_traces` green through
    the worker.
  - Files: `runtime/temporal/activities.py`, `tests/fitness/test_trace_completeness.py`.
  - **Done. Found first: every span emitted in an activity was lost.** A turn run by
    `handle` returns its tracer to its caller. In an activity the caller is the workflow,
    which must never hold spans (they'd be history, T48). `run_turn` dropped the turn's
    tracer, and `commit` dropped its own.
  - **Layer 9 gains a port:** `SpanSink`, with `CollectingSink` (tests) and `LoggingSink`
    (one JSON line per span on the `agentstack.traces` logger; stdlib, no dependency; a
    trace backend is deployment's choice later). `RunActivities` takes `traces` as a
    **required** argument, so losing spans can't be the default again. `agentstack-worker`
    and the SIGKILL worker pass `LoggingSink`; the test helpers default to a collector.
  - `run_turn` exports the turn's spans. `commit` exports in a `finally`, so a refused act
    is traced too, and a refusal is the trace someone asks for. Every span carries the
    run's `run_id` and `session_id` from the record, never a Temporal id.
  - **History is never read as audit:** an AST scan finds no history API
    (`fetch_history*`, `WorkflowHistory`, `Replayer`) anywhere in `src/agentstack`. Only
    `scripts/replay_guard.py` reads history, outside the package.
  - 5 tests. `tests/durability/test_traces.py` (in `durability`, not
    `test_trace_completeness.py`, because it needs that directory's dataset fixture and
    the time-skipping server): a run through the production worker emits every
    `REQUIRED_SPANS` member, all with our ids, none mentioning the workflow or Temporal
    run id; a refused act is traced (the second read of the rollout's world is the
    commit's); the logging sink writes one JSON line per span.
    `test_audit_separate_from_traces` +2: the scan, and a plant proving it sees a reader.
  - **Mutation-checked** on an isolated Postgres: without the commit's export, both run
    tests fail. The first draft of the refusal test **passed** under that mutant (the
    rollout turn's own spans satisfied it), so it now asserts the commit's specific span.
  - **Not traced, as before:** `evaluate_cycle`, `ask_approval` and the wait activities
    emit no spans. The ask has no version stamp without a turn host, and adding one only
    for it wasn't worth another constructor argument. A turn that raises mid-way loses
    its spans, because `loop.run_turn` owns its tracer.

- [x] **T50 — `operator status` joins position and record** · layers 3, 1 · *S*
  - Acceptance: status shows the workflow's position (Temporal) beside the run's record
    (Postgres); `stalled` still reads only `waits`.
  - Verify: `test_stalled_waits` operator tests green; a new status test for a parked run.
    Status flags a pending activity past 10 attempts, with its last failure, because
    the retry policy has no cap by design (decided at Checkpoint I).
  - Files: `runtime/operator.py`, `interfaces/operator_cli.py`, test.
  - **Done, not in `runtime/operator.py`:** that module is trigger evaluation, not an
    operator view. Position is read in `runtime/temporal/client.py` (`position()` →
    `Position`), and the join is in `operator_cli.status_report`.
  - `agentstack-operator status RUN_ID [--address]`:
    - **Record lines** (Postgres): the run (tenant, stage, session) and each pending
      wait (kind, id, deadline, re-asks).
    - **Position lines** (Temporal): workflow status; what it awaits (approval, and how
      often it was put; reconciliation; a trigger wait, and if overdue); cycles, acts
      and queued triggers; each pending activity and its attempt.
    - Neither is taken for the other.
  - **Built for the moment it's used, a dead worker:** the server's `describe` answers
    with no worker. The progress query needs one, so it's bounded (5s) and says "no
    worker answered" instead of hanging. The record is read first: an unknown run exits 1
    without contacting Temporal.
  - **Stuck:** a pending activity past `retry.STUCK_AFTER_ATTEMPTS` (10, beside the
    policy it qualifies) is flagged `STUCK` with its last failure, and the exit code is 1
    so a scheduler can alert. It's not a cap: the activity keeps retrying.
  - Also closes part of T43's gap: a reconcile wait now shows, as a record wait with no
    deadline and as "awaiting reconciliation". `stalled` is unchanged and still reads only
    `waits` (all 29 of its tests green). Reconcile waits aren't in `stalled`, and there's
    still no reconcile command.
  - 4 tests in `tests/durability/test_operator_status.py`: a parked run's record and
    position side by side; no worker alive → still answers; an evaluation failing every
    attempt → retried past 10 on the time-skipping server (backoff is skipped, not slept),
    flagged with "the scoring service is down", exit 1; an unknown run → exit 1, with an
    address nothing listens on proving Temporal wasn't asked.

- [x] **T51 — Ledger and bar** · docs · *S*
  - `STACK.md` rows 3 and 10 (Temporal owns position, Postgres the record); `CONSTRAINTS.md`
    additions only: floor bullets "No identity envelope, credential or prompt in
    workflow history", "No idempotency key derived from Temporal identity", "No gateway
    refusal retried"; enforced rows for the replay guard and `test_temporal_boundaries`;
    fitness count ratchet; README counts.
  - **Done.** `STACK.md`:
    - Row 3: Postgres is the record, Temporal the position, and the Phase 8 invariants
      (contract 6, declared activities, ids-only, the world read at the act, nothing
      secret in history, the replay guard).
    - Row 10: Temporal is substrate for position only, since history is retained for a
      window and rewritten by reset.
    - Row 9: T49's `SpanSink`, and that nothing reads history.
    - The Runtime, workflows, durable execution row names `test_temporal_boundaries`, `tests/durability/` and the replay
      guard.
  - `CONSTRAINTS.md`, additions only: the three floor bullets, plus two for invariants
    this phase built:
    - "No approval checked against a snapshot carried to the act" (T43, E3).
    - "No span emitted in an activity left for the workflow to hold" (T49).
    - Enforced rows for durable-runtime boundaries and replay safety, and the fitness
      ratchet 36 → 42.
  - README: it said **five** `.importlinter` contracts while there were six (contract 6,
    T33), and nothing checked it. Now `test_the_readme_counts_match_what_is_actually_here`
    checks the contract count too, and it fails on "five". Also corrected: the durability
    row (Temporal, not Postgres alone); the scripts row; and a "LangGraph is not wired in"
    bullet, false since T8, replaced by the real state and the open reconcile gaps.
  - `stack_guard`: the bar is intact.

- [x] **Per-run test databases** · layer 10 · *S* (added 2026-09-28, owed since T22)
  - **Why:** every run on a machine rebuilt the same `agentstack_app_test`, and each
    rebuild dropped it under every other run. Sessions sharing the dev Postgres failed
    whole suites (`AdminShutdown`, rows vanishing), and every Stop hook could fail on
    somebody else's run. T23, T43, T44, the merge and T48–T51 were each verified on a
    throwaway container to get a result worth reporting.
  - `storage.provision` (layer 10):
    - `run_scoped(name)` inserts one token per process before the disposable suffix:
      `agentstack_app_<token>_test`.
    - `drop_database` is under the same disposable-name rule as `rebuild_database`,
      checked before any connection.
  - `tests/conftest.rebuild()` scopes and records every name, and a session fixture
    drops them all when the run ends. The Temporal task-queue token is the same token.
    The main suite, `tests/infra`, `blank_test`, `checkpoint_order_test` and the evals
    runner all go through it: no fixed database name is left.
  - 7 tests in `tests/infra/test_provision.py`:
    - the suffix stays last, and one process always gets the same name;
    - **two real processes never share a name**;
    - both new functions refuse a non-disposable name before connecting;
    - a dropped database is gone;
    - the suite runs on its own database.
  - **Proved on the shared dev Postgres:** two full suites at once, **712 passed each**,
    with two scoped databases live side by side and **0** left afterwards. Then
    `check_full.sh` on the shared database: all green, 13/13 gates.
  - Fixed-name databases left by older code are still on the dev server. Sessions not
    yet on this code use them, so they were left alone.

### ✅ Checkpoint M — Phase 8 complete
- [x] Spec criteria C29–C45 met; SPEC.md criteria 1–28 still hold
  - C29 one run per id: `test_temporal_boundaries` (T34). C30 redelivery after close:
    Checkpoint J / T36. C31 death while parked: `test_approve_after_death` (T44).
    C32 re-ask over 72h: `test_approval_wait` (T41). C33 stale approval refused at the act
    and C34 unresolved effect parked, reconciled, deduplicated: `test_commit` (T43).
    C35 refusals are one attempt and C37 only declared activities run:
    `test_temporal_boundaries` (T35). C36 keys survive Temporal: T39, T40, and T43's
    rerun. C38 workflow code has no path to an effect: contract 6 + `stack_guard` (T33).
    C39 an outsider changes nothing: `test_slack_answer` (T42) + `test_commit` (T43).
    C40 nothing secret in history: T48. C41 a stranding deploy fails: `replay_guard` (T47).
    C42 bounded fan-out: `test_concurrency` (T45). C43 stalled still surfaces:
    `test_stalled_waits` + `test_timers`. C44 continue-as-new invisible: `test_timers`
    (T46), including `operator status` showing one run with all 250 cycles (added at this
    checkpoint). The *traces* half holds by construction, since spans are tagged with the
    carried `run_id`; no test drives a turn across a handoff. C45 the substrate fails
    loudly: `tests/infra/test_temporal_substrate` + `worker_cli` (T32, T34).
  - SPEC.md 1–28: held by the full suite, **705 passed** on an isolated Postgres.
    `stack_guard` finds no fitness test removed or weakened.
- [x] `check_full.sh` green, including `replay_guard` and `checkpoint_guard`
  (2026-09-28, isolated Postgres: every check ok, changed-line coverage 89.6%, 13/13
  release gates; `osv-scanner` and `gitleaks` not installed locally, CI runs them)
- [ ] `/stack-audit` run and its findings addressed
  - **Run 2026-09-28 on `59ba94a..c333575`. Verdict: do not ship.** 1 Critical, 5 High,
    7 Medium, 7 Low. Gates were green before it ran. The full audit is in the
    conversation record. The fixes, grouped by what they touch (human decision: fix C1,
    all Highs, M1 and M7 now; the other Mediums and the Lows are follow-ups):
- [x] **A1 — What a person is asked about is what commits** (audit C1, H2, H3, H4) · layers 8, 3, 9, 10
  - C1: a rollout proposal that differs from the frozen cohort is refused before
    anyone is asked; the headline and headcount come from the recorded action, not a
    constant. (Human decision kept from T43: the model's proposal is what commits.)
  - H2: a wake-up with the wait still unanswered doesn't advance the run: `not_answered`,
    recorded, and the run goes back to waiting and re-asking.
  - H3: every human answer (yes or no) and every refusal before the gateway is an audit
    record.
  - H4: the prepared action is stored with the wait (migration 0018, renumbered from 0015 at the merge with `main`); the commit reads
    it from the record, not from the LangGraph checkpoint.
- [x] **A2 — Unresolved effects park where a person sees and settles them** (audit H1, M1, H5) · layers 7, 3, 1
  - H1: no effect moves its own world snapshot (a draft did); catalog-wide fitness test.
  - M1: an `UnresolvedEffect` in the turn path parks a reconcile wait too.
  - H5: reconcile waits get a deadline, `operator stalled` reports them and exits 1,
    and an `agentstack-operator reconcile` command settles the claim, satisfies the wait,
    audits and wakes the run (migration 0019, first written as 0016).
- [x] **A3 — Docs claim only what the code enforces** (audit M7), after A1 and A2 merge · layers 3, 5, 7, 8, 9 · *S*
  - **Done 2026-10-01.** STACK.md rows 1, 3, 7 and 9 and the CONSTRAINTS E2 and
    unsatisfied-wait bullets were already accurate (the latter held by
    `test_wait_gates_the_run` and the `not_answered` cases in `tests/durability/test_commit.py`);
    the spec's layer ledger was not. Checked claim by claim against the code, and
    corrected in `SPEC.md`:
    - **Layer 8: "approval mints an envelope that has the rollout scope" was false.** The
      envelope is minted per turn from the run's *stage* (`STAGE_SCOPES`, 15 minutes,
      revocable); approval is a separate check at the gateway, bound to the payload
      fingerprint. Rollout authority is those two things, neither standing in for the other.
    - **The ledger named a test that did not exist** (`test_approval_widens_authority`), and
      nothing referenced `STAGE_SCOPES` at all, so "an evaluation run holds no rollout scope"
      was true and unproven. `tests/fitness/test_stage_authority.py` (7 tests) now holds it;
      sabotage-verified (granting the draft stage the rollout scope fails two of them).
    - **Layer 5: "prior decisions are durable memory" was false.** `MemoryStore` is
      in-process and nothing writes it. Now stated as not built; decisions live in registry
      events and the audit trail, which are records, not memory. (The gap itself is still open.)
    - **Layer 7: "two surfaces, registry and rollout API" was false.** One surface; a rollout
      is a guarded move to `live` in the registry (ADR-0010).
    - **Layer 3:** four wait kinds, not two. **Layer 9:** spans are per turn stage and gateway
      step, not per cycle; evaluate/ask/answer are spanned by `CYCLE_SPANS` (M3, fixed).
    - `context/evidence.py` is `context/assemble.py`; the envelope no longer claims to be
      bound to one variant and percentage (the approval's fingerprint is).
  - `README.md` fitness count 46 → 48, which `test_the_readme_counts_match_what_is_actually_here`
    required once the two new test files existed.
- Follow-ups: M2–M6 done (2026-10-01): M2 trigger tenant tested in layer 8 against the
  run (`authorize_trigger_tenant`; the source-to-tenant authority gap is still unrecorded),
  M3 `CYCLE_SPANS` for evaluate/ask/answer, M4 a re-ask re-checks the world and withdraws a
  question that moved, M5 a cancelled run ended cancelled (the workflow was reading the
  cancel as a refusal; turn and heartbeat were already correct), M6 a stale approval ends
  visibly (approver told, named in `operator status`). Open: L1–L7.
  Merging with `main` (C2, H1, H3 there; `test_concurrency`) is its own task after these.
- [ ] Human review

- [x] **A1 — What a person is asked about is what commits** · layers 8, 3, 9, 10 · *M*
  (audit findings C1, H2, H3, H4; added 2026-09-28)
  - **C1 (critical). What the approver read was not what the approval bound.** The ask
    headlined a constant `ROLLOUT_PERCENTAGE` and a headcount built from it, while the
    approval and the act bound the fingerprint of whatever the model proposed. Nothing
    held the proposal to the frozen cohort, and `percentage` is an unbounded integer.
    So a proposal of 100%, or a looser threshold, would have been shown as "~4 customers
    (10%)" and then rolled out as proposed.
    - Kept from T43: the model's proposal is what commits. It now has to be the frozen
      cohort's rollout first. `drafting.intended_rollout(cohort)` is the one rollout the
      record allows, and the instruction is built from it. `rollout_deviation` compares a
      prepared request with it by fingerprint and names each argument that differs.
    - The rollout turn gets a `rollout_admission`, passed through `run_turn(admit=...)`
      into `carried`, so `TurnContext` and the checkpoint shape don't change. `act`
      refuses a deviating proposal after `prepare` and before the gateway, so no wait is
      parked and nobody is asked. The refusal is a `tool.reject` span and an audit
      record, `proposal.deviates`, under the agent.
    - The ask and the act check the wait's action against the cohort again
      (`ProposalDeviates`, audited, non-retryable). This catches a wait parked some
      other way.
    - The question is sized from the action the wait holds: `Asker.ask(action=...)`, and
      `ChannelAsker` reads the percentage and version from its payload. No constant is
      left in the ask.
  - **H2. A stray `answered` signal moved the run past an unsatisfied wait.** `commit`
    now reads the wait first. If it is unsatisfied, the activity writes a
    `commit.not_answered` span and a `wait.unsatisfied` audit record, and returns
    `not_answered`. Nothing reaches the gateway. The workflow spends that one wake and
    goes back to the same question on the re-ask timer, without posting it again. A
    later genuine yes commits.
    - `test_commit.py:224` asserted that the wait was still unanswered *after the run had
      moved on*, which enshrined the bug. It now asserts the correct behaviour (renamed
      `test_a_wake_up_nobody_answered_moves_nothing_and_a_later_yes_still_commits`).
      That is a fix, not a weakening.
    - `test_approval_wait::test_an_answer_stops_the_asking` used a bare signal as the
      "answer". It now records a real answer before signalling.
  - **H3. A "no" left no accountability record.** `ApprovalCoordinator` has an audit sink
    now, and writes a record for every answer: `human.approved`, `human.refused`,
    `approver.not_authorized` for an outsider (under `slack:<claim>`, never promoted to
    a person), and `answer.not_applicable` for a second click. Each names the approver,
    wait id, fingerprint and snapshot. The records survive session deletion, which the
    wait's payload doesn't.
    - The commit activity audits its own refusals before the gateway, each with a
      `commit.refuse` span: `NothingApproved`, `WorldUnreadable` and `ProposalDeviates`.
      `NothingApproved` had neither before.
    - `RunActivities` requires `audit`, just as it requires `traces`. Audit and traces
      stay separate sinks.
  - **H4. The approved action lived only in the LangGraph checkpoint.** `migrations/0018`
    (first written as 0015) adds `waits.action_tool` and `waits.action_arguments` (both or neither), plus
    `audit.records.wait_id` and `state_snapshot`. `act` parks the validated arguments
    with the wait. `commit` reads them from the wait, prepares them again through the
    registry (schema and exposure), and holds them to the recorded fingerprint. It
    never reads the checkpoint. Approval waits parked before 0018 hold no action and
    are refused at the act, with an audit record, rather than backfilled.
  - **Found:**
    - The workflow's reconcile loop has the same shape as H2. A stray signal naming a
      reconcile wait makes `_act` commit again. The gateway answers `unresolved` and the
      same reconcile wait is re-parked, but the wait id stays in `_answered`, so the loop
      spins on activities without a timer. It is not fixed here because A2 edits the
      same loop.
    - `data/manifest.json` is not tracked on this branch, so a fresh worktree fails
      `test_data_snapshot` until `data/` is present.
    - `stack_guard` and `checkpoint_guard` with `--base main` report findings that come
      from `main` being ahead of this branch (`request_fingerprint`, the envelope fields,
      four CONSTRAINTS rows), not from this diff. Against the task base both are clean.
  - Tests: **15 new** (727 collected, was 712).
    - `test_proposal_admission.py` (8): four deviations never parked or asked, and
      audited; the headline equals the payload that commits; the asker sizes the question
      from the action; the ask refuses a deviating wait; the commit refuses one even when
      approved.
    - `test_commit.py`: a wait not holding its action whole is not committed (×3, which
      replaces the "never proposed" test); the act commits with no checkpoint at all;
      the rewritten :224 test; `WorldUnreadable` and a "no" are now audited.
    - `test_slack_answer.py`: every answer is a record that outlives the session (×3);
      a second click is a record.
    - `test_stalled_waits.py` now parks what a turn parks (the real rollout and its
      action), with its assertions unchanged. The `refused_at_the_act` history scenario
      is now a stray wake, then a "no".
    - Two gate evals: `a-deviating-rollout-never-reaches-a-person` (C1) and
      `a-wake-nobody-answered-moves-nothing` (H2), bringing the total to 15/15.
  - **Mutation-checked:** each fix reverted alone (11 reverts) fails its test on an
    assertion or a refusal; both evals fail under their reverts.
  - Histories re-recorded; the old four replayed against the new code first.
    `check_task.sh` and `check_full.sh` green. Changed-line coverage 96.7%, project 98%.
- [x] **A2 — Unresolved effects park where a person sees and settles them** · layers 7, 3, 1 · *M*
  (audit findings H1, M1, H5; added 2026-09-28)
  - **H1 (a regression from T43): a lost answer on the draft wedged the run for good.**
    `act` bound *every* describable resource to the world its surface describes. The
    draft creates the experiment the registry then describes, so its snapshot was
    `None` before its own effect and a description after. Killed between the commit and
    the step's record, the rerun read its own policy grant as stale before the ledger
    could deduplicate, parked a `human_approval` wait for a `PRE_COMMIT` act, and the
    workflow (0 receipts) never asked. That pending wait blocked every later turn.
  - **Shape chosen: only an `ALWAYS` action is bound to the world**
    (`runtime/snapshot.binds_the_world`, `approval_snapshot`, used by `nodes.act`).
    - That tier is the one a person answers later, about a world the commit reads again
      at the act. E3 ("no approval checked against a snapshot carried to the act") holds
      for the rollout unchanged.
    - A `PRE_COMMIT` grant is minted by a rule *at* the act, so there's no later world to
      compare, and its turn-derived snapshot is the same on a rerun.
    - Rejected: a per-tool "moves its own snapshot" flag. It's the property to *test*,
      not to declare: a declaration can be wrong, and the fitness test below asks the
      surface.
    - Rejected: asking the ledger before the approval. That reverses the Identity, trust, policy, approvals order in
      the gateway, and a deduplicated repeat would skip the authorisation check.
  - **M1: an `UnresolvedEffect` in the turn path parked nothing.** The claim stayed
    IN_FLIGHT, and only `unresolved_keys()` knew.
    - `act` now parks a reconcile wait (`waits.park_reconcile`, shared with the commit
      path) and re-raises. The turn's checkpoint stays before `act`.
    - The `run_turn` activity reports `UNRESOLVED` with the wait. The workflow waits on it
      (`_settled_turn` → `_reconciled`) and then takes the *same* turn again: the model
      isn't asked again, and the ledger answers for that proposal.
    - Outside Temporal, the next turn is blocked on the wait (two fitness tests changed
      from "raises again" to "blocked, and the surface still called once").
  - **The reconcile wait is one per claim, not per action** (`reconcile_wait_id(run, key,
    claimed_at)`; `UnresolvedEffect` now carries the key and the claim's `claimed_at`).
    With the old id, a key released as "not applied" and then lost again would re-park
    the old, *satisfied* wait, and the run would spin on it.
  - **An answer is spent once** (`_spent`). A wake-up for a claim nobody settled went
    round the act in a tight loop: 164 attempts in a second, measured on the mutant.
  - **H5: a reconcile wait had no deadline, no command and no alert.**
    - `migrations/0019` (first written as 0016): `waits.idempotency_key`, plus two new CHECKs. A pending reconcile
      wait has a deadline (0010's CHECK exempted every kind it didn't name, and
      `reconcile` came later) and names its claim. Old rows are backfilled due-now, as
      0010 did.
    - `RECONCILE_DUE_AFTER` is 1h.
    - `operator stalled` lists every pending reconcile wait from the moment it parks,
      with its claim and the command to settle it, marked OVERDUE past the deadline. It
      has its own count and exits 1.
    - `operator reconcile WAIT_ID (--applied RECEIPT | --not-applied) --operator --reason`
      has its logic in layer 3 (`runtime/reconcile.settle`). It settles the claim in one
      conditional statement (`IdempotencyLedger.reconcile`), writes an audit record
      (principal `operator:<name>`, `reconcile.applied|not_applied`, the act's own
      surface and resource), then satisfies the wait. After that it wakes the run with
      the existing `notify_answer` signal.
    - Running `reconcile` again finishes a settlement that died halfway and changes
      nothing otherwise. A contradiction with the ledger is refused.
  - **Placeholder `0015`:** the migrator refuses a gap, and 0015 belongs to a parallel
    task. A no-op `0015_reserved_for_a_parallel_task` keeps this branch applicable. Drop
    it at merge. (Dropped: `main`'s `0015` is the C2 registry-move key, so A1 became
    `0018` and A2 `0019`.)
  - Tests:
    - `tests/fitness/test_effects_keep_their_snapshot.py`, new (catalog-wide): no tool's
      own effect moves its approval snapshot, the rollout is still bound to the world,
      and only `ALWAYS` binds. Fitness ratchet 42 → 43.
    - `test_worker_death`: SIGKILL after the draft commits and before its step is
      recorded. A fresh worker deduplicates and proposes the rollout.
    - `tests/durability/test_reconcile.py` (15 tests):
      - a lost draft answer parks, is settled applied or not applied, and the same turn
        resumes;
      - a forged wake-up doesn't spin;
      - the command: twice is once; not applied releases the claim; contradictions are
        refused; Temporal unreachable → "not woken", exit 1;
      - a settlement with no operator, no receipt, a stray receipt, a released claim or
        a keyless wait is refused.
    - `test_stalled_waits` gets 7 more; `test_unresolved_effects` gets 2 more.
    - Eval gate `a-draft-whose-answer-was-lost-does-not-wedge-the-run`.
    - New recorded history `draft_unresolved_reconciled`, and all five re-recorded.
  - Mutation-checked, each failing the tests named above:
    - the old snapshot rule (the fitness test, the SIGKILL test and the eval);
    - never binding the world;
    - no parking in `act`;
    - the old "answered" condition;
    - no reconcile kind in `stalled`;
    - no default deadline;
    - no 0019 CHECKs;
    - no wake-up in the command.
- [x] **Merge A1 + A2** · layers 3, 7, 8, 9, 10 · *M*
  - **What conflicted:** `evals/runner.py`, `migrations/README.md`,
    `src/agentstack/runtime/{loop,waits}.py`, `src/agentstack/runtime/temporal/
    {activities,contracts,workflows}.py`, `tasks/todo.md`. A prior pass had already
    resolved everything but `tasks/todo.md` in the working tree (no conflict markers
    left); this pass verified those resolutions rather than redoing them, then resolved
    `tasks/todo.md` by keeping both checkpoint entries in order.
    - `contracts.py`: kept both `NOT_ANSWERED` (A1) and `UNRESOLVED` (A2), both on
      `TurnOutcome`/`CommitOutcome`.
    - `waits.py`: `Wait` carries A1's `action_tool`/`action_arguments` alongside A2's
      `idempotency_key`; `park_reconcile` and `reconcile_wait_id` (A2) sit beside
      `approval_wait_id` (A1, pre-existing).
    - `loop.py`: `run_turn` takes both A1's `admit=` and A2's `tracer=`.
    - `activities.py`: `RunActivities` requires `audit=` (A1/H3); `run_turn` catches
      `UnresolvedEffect` and reports `TurnOutcome(status=UNRESOLVED, wait_id=...)` (A2/M1);
      `commit` reads the wait first and returns `not_answered` before the gateway
      (A1/H2), and `_act` parks a reconcile wait via `park_reconcile` on `UnresolvedEffect`
      (A2/M1); `_recorded`/`_approved`/`_refuse` (A1/H4, H3) are unchanged by A2's part.
    - `workflows.py`: `_propose`'s outer loop re-asks on `NOT_ANSWERED` (A1/H2) while
      `_settled_turn`/`_reconciled`/`_spent` (A2/M1) own the reconcile wait inside `_act`;
      both `NOT_ANSWERED` and `UNRESOLVED` are handled as distinct exits from `_act`'s
      inner loop.
    - `migrations/`: A2's placeholder `0015_reserved_for_a_parallel_task` does not exist
      in the working tree or `migrations/README.md`. At this merge A1 and A2 were
      `0015` and `0016`; they were later renumbered to `0018_waits_hold_the_action` and
      `0019_reconcile_waits_are_watched` after `main`'s `0015`–`0017` landed.
      `migrations/README.md` lists them under the current numbers.
  - **The reconcile-loop bug A1 flagged:** real as a class of bug, but already fixed by
    A2's own design before the merge, and the fix survives the merge unweakened. Traced
    through the merged `_act`/`_reconciled`: a stray `answered` signal on a reconcile
    wait id appends once to `_answered`; `_reconciled` compares `_answered.count(wait_id)`
    against `_spent[wait_id]` rather than testing membership, so one stray signal is
    consumed once and then the loop blocks on `wait_condition` again — it does not
    re-enter `_commit` without a timer. Confirmed by running the existing spin test,
    `tests/durability/test_reconcile.py::test_a_wake_up_nobody_settled_anything_for_does_not_spin_the_run`
    (a forged `notify_answer` signal on the reconcile wait, then a 1s sleep "long enough
    for a spinning run to have gone round", asserting exactly two `UNRESOLVED` commits
    before the real settlement lets the third dedupe): **passed**, 15/15 in
    `tests/durability/test_reconcile.py`. No code fix was needed for this bug on the
    merged tree.
  - **Left open:** the full gate sequence in the merge instructions
    (`check_task.sh`, `check_full.sh`, `stack_guard.py --base c333575`,
    `checkpoint_guard.py --base c333575`, `replay_guard.py --record` +
    `replay_guard.py`, fitness/coverage counts, and the merge commit's own gate run) was
    not completed in this pass — stopped short on instruction, after the substrate was
    brought up and the targeted reconcile test above was run and passed. Whoever
    continues this needs to: re-record the five replay histories (including
    `draft_unresolved_reconciled`) on the merged workflow, confirm
    `tests/fitness/test_temporal_boundaries.py` and the README's fitness count against
    CONSTRAINTS.md's ratchet, and run the full gate list before this merge is
    considered shippable.
  - **Gate run (commit 272e4b5, after re-recording histories):** all green, no code
    fix was needed beyond the re-recorded histories.
    - Re-recorded all 5 histories (`replay_guard.py --record`), including
      `draft_unresolved_reconciled`; `replay_guard.py` (no `--record`) then passes:
      "5 recorded histories replay against this code".
    - `tests/fitness/test_temporal_boundaries.py`: 23/23 passed, no secrets in the
      decoded payloads.
    - README fitness count vs. CONSTRAINTS.md ratchet: already matched reality — 43
      files under `tests/fitness/test_*.py`, README says "43 tests", ratchet says 43;
      `test_the_readme_counts_match_what_is_actually_here` passes. No edit needed.
    - `stack_guard.py --base c333575`: "the bar is intact". `checkpoint_guard.py --base
      c333575`: "v1 is safe to ship over v1".
    - `check_task.sh`: fully green — ruff/format/mypy/lint-imports/capabilities ok;
      373 tests passed (98% coverage, matching the CONSTRAINTS.md ratchet); changed-line
      coverage "no changed executable lines in src/" (only fixture JSON changed);
      stack_guard and checkpoint_guard clean; replay_guard clean.
    - `check_full.sh`: green through release gates — 16/16 Part-8 gate cases pass;
      `osv-scanner` and `gitleaks` skipped (not installed locally; CI enforces both).
    - The four suite/file ERRORs reported from the half-merged tree
      (`tests/infra/test_registry_store.py`, `tests/infra/test_temporal_substrate.py`,
      `tests/test_cli_smoke.py`, `tests/test_validation_and_ledger_edges.py`) and the
      36% changed-line-coverage reading do **not** reproduce on the merged, quiet tree:
      52/52 passed running those four files together in isolation. Consistent with the
      task's own hypothesis — two suites racing on the half-merged tree, not a real
      regression. One transient failure was seen in
      `tests/durability/test_timers.py::test_continuing_as_new_is_invisible_to_the_record`
      while two full suites ran back-to-back; it passed in isolation and on a clean,
      single full run (373/373), so treated as load-induced flake, not a merge bug.
    - Commit: `272e4b5` "Re-record the replay histories on the merged A1+A2 workflow".
      No separate fix commit was needed — the merge itself had no gate-breaking bug.

- **ADR-0010 follow-ups closed.**
  - `evals run --gates` fixed (runner passes `prior_rollout_event`; two stale cases repaired:
    `experiments:write` → `experiments:draft`, and two frozen-cohort messages gained
    `prior_rollout_event=0`). 20/20 gate cases pass.
  - `CONSTRAINTS.md` / `README.md` conflicts resolved (fitness count recorded as 45, the
    real file count at the time; it is 46 now, and the README says 46).
  - `test_a_turn_longer_than_its_heartbeat_timeout_is_not_retried_while_alive`: not
    reproduced in 3 clean runs and 3 runs under 12 CPU burners. Left unchanged; treated as
    an unproven one-off.
  - Spikes frozen with a README; `Surface.API` / `RecordingClient` retained (ADR-0010).
  - `tests/live/test_ollama.py` run against `qwen3:8b`: passes.

## ADR-0011 — Slack Bolt receiver (`3ee98a4`, added 2026-09-30)

Recorded here because the commit landed without a ledger entry. Read
`docs/adr/0011-slack-bolt-receiver.md` for the reasoning.

- [x] **Bolt approval receiver** · layer 1 · `interfaces/slack_app.py`, `slack_cli.py`
  - Bolt hands the raw bytes to our own signature check and replay guard; it does not
    replace them. Tests: `tests/fitness/test_slack_app.py`.
- [x] **`awaiting_approval` step status** · layer 3 · `migrations/0020`, `runtime/steps.py`
  - `step()` takes `pause_on`, so only the turn path (which parks a wait) records the
    new status; a refusal or stale approval at the commit activity is recorded as
    `failed`. The commit activity now runs the act as a recorded step under the shared
    `execute_step_name`. Tests: `test_step_identity.py`, `tests/durability/test_commit.py`.
- [x] **Recorded-score worker** · layer 4b · `prediction/churn.py` (`RecordedScorers`),
  `worker_cli --scores`
  - Replays recorded scores without the weights. Tests: `test_prediction_gate.py`.

## State check, 2026-09-30

`tasks/todo.md` was reconciled against the code. Corrected: migration numbers for A1/A2
(0018/0019), fitness count (46), A1/A2/T49 checkboxes, recorded scores now present,
per-invocation test databases done. Verified by running: `evals run --gates` 20/20.
**Not re-run in this pass:** the full suite, so the pass counts quoted in older entries
are historical. Still open: A3 (partial), the `/stack-audit` re-run, every "Human review"
box, Checkpoint F, the audit's M2–M6 and L1–L7 follow-ups, the merge with `main`, the
per-run lease decision (T21), the `MemoryStore` durability gap (Checkpoint A), PII in
traces, and secrets in environment variables.
