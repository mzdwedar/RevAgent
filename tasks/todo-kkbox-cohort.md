# Tasks: KKBox cohort

Plan: `tasks/plan-kkbox-cohort.md`. Spec: `SPEC-kkbox-cohort.md`. Separate from
`tasks/todo.md` (Experiment Operator iteration 1).

Standing bar on every task (`CONSTRAINTS.md`): `check_fast.sh` per edit, `check_task.sh` at
turn end, changed-line coverage ≥ 80%, no weakened bar (`stack_guard`), no suppression
comments. Each task names its **layer** and its **proving test**; sizes: XS/S/M (nothing
over ~5 files).

**Preconditions (yours, not tasks):** Kaggle competition rules accepted for
`kkbox-churn-prediction-challenge` and `~/.kaggle` set (needed from K11a); `TABPFN_TOKEN` in
`.env` valid for the local model's gated weights (`TabPFNScorer.preflight` proves it; first used in K12).
**Amended 2026-10-02:** KKBox is scored locally; no row leaves the machine (ADR-0012 §6). K12 and
K14 are rewritten accordingly; K7-K10 are done and stay, for cohorts cleared to go to the hosted
service. The spec is amended to match (plan Open questions 3). `TABPFN_TOKEN` is read from `.env`
by `record_scores.py`, `agentstack-preflight` and (through its preflight) the worker; a variable already
exported wins (plan Open question 5, resolved).

---

## Phase 0: look at the data first (reordered 2026-10-01 at your request)

Order is now **K11a (fetch + explore) → Checkpoint E → K4 (RC mapping)**, then the rest of
Phase A. K4 is designed against what the real data shows, not against what the schema
promises (cutoff, left-censoring rate, trial prevalence, `is_cancel` semantics). K11a no
longer depends on K6: the exploration is a standalone script. K11a also creates
`tests/fixtures/kkbox/` and `tests/fitness/test_kkbox_mapping.py` (candidate-target
tests); K4 extends both. K1–K3 (currency) are independent and can run any time before K6.

(K11a and Checkpoint E are specified below, in Phase C, where they were first written;
their position in the *order* is here.)

---

## Phase A: pure, provable in CI with no data, token or network

- [x] **K1: `currency` on the cohort spec** · layer 5 · *S* — **Done 2026-10-01.** `DatasetSpec.currency`
  (default `USD`); `targeting.money()`; `Cohort.currency`; `description()` carries `currency` only when
  non-USD (so USD records and `experiment_version` are byte-identical; pinned by literal
  `exp:9c93736ad3fe0a25`); `FrozenCohort.currency` reads it (absent → USD). No migration. 4 tests added.
  - Acceptance: `DatasetSpec.currency` (default `"USD"`); the cohort `targeting` produces
    and `frozen_cohorts` carries it; targeting's refusal text uses it instead of `$`. For a
    USD cohort every existing value, message and **experiment_version fingerprint is
    byte-identical** to before.
  - Verify: `uv run pytest tests/fitness/test_targeting.py tests/fitness/test_data_snapshot.py`
    extended (a non-USD fixture spec renders its code; USD fingerprint unchanged), plus
    `uv run python scripts/replay_guard.py` and `scripts/checkpoint_guard.py --base main`.
  - Depends: none. Files: `datasets.py`, `targeting.py`, `frozen_cohorts.py`, test (~4).

- [x] **K2: currency in the run's cohort summary** · layer 3 · *XS* — **Done 2026-10-01.** `operator.py`
  uses `targeting.money(..., cohort.currency)`; USD string pinned (`$24,000 at risk`); replay and
  temporal-boundary tests green.
  - Acceptance: `runtime/operator.py:110` renders the cohort's currency, not `$`; USD output
    unchanged.
  - Verify: extend `tests/fitness/test_trigger_to_candidate.py` (non-USD cohort shows its
    code; USD string identical); `test_temporal_boundaries.py` and `replay_guard.py` stay
    green (workflow history unchanged).
  - Depends: K1. Files: `operator.py`, test (~2).

- [x] **K3: currency in the approval prompt** · layer 1 · *S* — **Done 2026-10-01.** `ApprovalAsk.currency`
  (default USD, formatted inside layer 1, no import of layer 5); `ChannelAsker` passes
  `cohort.currency`; headline still names a headcount. 2 tests in `test_slack_outbound.py`, 1 in
  `test_proposal_admission.py` (frozen-cohort → ask).
  - Acceptance: `interfaces/slack.py` (and `wiring.py`'s pass-through) renders the cohort's
    currency; the prompt **still names a headcount, never a percentage**; USD unchanged.
  - Verify: `uv run pytest tests/fitness/test_slack_outbound.py tests/fitness/test_approval_legibility.py`
    extended (NTD prompt shows `NTD` and a headcount; no bare percentage).
  - Depends: K1. Files: `slack.py`, `wiring.py`, tests (~3).

- [x] **K4: KKBox transactions → RevenueCat events** · layer 5 · *M*
  - Acceptance: `context/kkbox.py` maps transactions to `RCEvent`s (`INITIAL_PURCHASE`,
    `RENEWAL`, `CANCELLATION`, `EXPIRATION`; `period_type`; `purchased_at_ms`,
    `expiration_at_ms`, `app_user_id`; `payment_method_id` in `extras`). First observed
    transaction is `INITIAL_PURCHASE` only within the window of registration, else `RENEWAL`
    (left-censored, counted). Derived `TRIAL` rule as in the spec. Starts `docs/adr/0012-…`
    with the mapping-fidelity decisions.
  - Verify: **new** `uv run pytest tests/fitness/test_kkbox_mapping.py` on a committed
    hand-built fixture (`tests/fixtures/kkbox/`): one case per event type, censored first
    transaction, trial, cancel, expiry; no data needed.
  - Depends: K11a + Checkpoint E (so the mapping rules are checked against real data); extends
    K11a's fixture and test. Files: `kkbox.py`, fixture, test, ADR (~5).

  - **Done 2026-10-01.** `agentstack.context.kkbox.to_events` (vectorised; ~50 s for the full
    cohort), 12 mapping tests added to `test_kkbox_mapping.py` (20 in the file), ADR-0012 started
    (decisions 1-3 decided, 4-7 proposed). On a 30k-user real sample the distribution matches the
    exploration (INITIAL 36.1% vs 36.1% measured; TRIAL 0.47% vs 0.4%; 2.1% of users end expired).
    `ordered`/`to_date` moved into `kkbox.py`; the explore script imports them. One judgment call
    flagged in the ADR: a lapse is strict (a renewal the day after expiry is EXPIRATION + RENEWAL),
    14.8% of events, because KKBox states no grace period.

- [x] **K5: one row per subscriber, at a cutoff** · layer 5 · *M*
  - Acceptance: per-user feature builder from the spec's Feature Set; population = users
    with a label and an expiry in the label window; `bd` outside 10–90 → null + missing
    flag; `msno` and raw date columns excluded with reasons; the raw registration/expiry
    dates are produced as *candidate* columns behind a flag (decided in K13). Cutoff is a
    parameter, not a constant, until K11 fixes it.
  - Verify: extend `tests/fitness/test_kkbox_mapping.py`: **a transaction after the cutoff
    changes no feature** (mutation test on the fixture); same input → same frame twice;
    label join correct; `bd` cleaning cases.
  - Depends: K4. Files: `kkbox.py`, fixture, test (~3).

  - **Done 2026-10-01.** `kkbox.to_features(events, members, labels, cutoff, *, raw_dates=False)`
    returns one row per labelled user with history (indexed by `msno`, plus `is_churn`); 26
    features in the spec's seven groups. Events past the cutoff are *refused*, not ignored; a
    registration after the cutoff is unknown, not negative tenure; `bd` outside 10-90 is null
    plus `bd_missing`; city/registered_via/payment method are categories; `msno` and the raw
    dates are in `EXCLUDED` with reasons; the two raw dates are `CANDIDATE_COLUMNS`, off unless
    `raw_dates=True`. 15 tests added (35 in the file); `kkbox.py` 100% line coverage. On a 30k
    real sample: 29,928 rows, churn 8.9%, max |corr| with the label 0.40 (days since last event);
    `bd` missing 60%, tenure unknown 11.5%.

- [x] **K6: register `kkbox-churn`, targetable** · layer 5 · *M*
  - Acceptance: `REGISTRY["kkbox-churn"]` (`files=("kkbox-cohort.csv",)`, target
    `is_churn`, drops with reasons, `revenue_columns` = last pre-cutoff `actual_amount_paid`,
    `revenue_periods_per_year=12`, `revenue_note`, `currency="NTD"`); `DatasetSpec.derived_by`
    makes `load` and `fetch_datasets.py` name `scripts/build_kkbox_cohort.py` when the file
    is absent instead of failing opaquely; `bank-churn` still `RevenueNotObserved`.
  - Verify: `uv run pytest tests/fitness/test_data_snapshot.py tests/fitness/test_targeting.py`
    extended with a frame built from the K5 fixture (same data → same `data_as_of`; kkbox
    targetable, value at risk in NTD; bank-churn refused); `test_the_bar_guards_itself.py`
    stays green.
  - Depends: K1, K5. Files: `datasets.py`, `fetch_datasets.py`, tests (~4).

  - **Done 2026-10-01.** `REGISTRY["kkbox-churn"]` (NTD, `last_actual_amount_paid` x12, `msno` dropped
    with `kkbox.EXCLUDED`'s reason); `DatasetSpec.derived_by` makes an absent file name
    `scripts/build_kkbox_cohort.py`; `fetch_datasets.py` skips derived cohorts until built. 6 tests
    added to `test_kkbox_mapping.py`. **Amendment, flagged:** `test_the_manifest_records_a_watermark_…`
    now exempts a *derived* cohort that is not yet in the manifest (no `data_as_of` exists before K11);
    downloaded cohorts and every recorded entry are still checked. **Until K11, `tests/live`
    parametrised over `REGISTRY` fails for `kkbox-churn`** with "Build it" (no cohort file yet).

### Checkpoint A: before anything leaves the machine
- [ ] `bash scripts/check_task.sh`, `uv run pytest tests/fitness`, `uv run lint-imports`,
      `scripts/stack_guard.py --base main`, `replay_guard.py`, `checkpoint_guard.py` green
- [ ] USD cohorts byte-identical (K1 proof); prompt shows NTD for KKBox (K3)
- [ ] ADR-0012 has mapping/feature/revenue decisions; `/layer_of.py --base main` shows only layers 1, 3, 5
- [ ] Human review

---

## Phase B: the hosted scorer (new side effect: egress)

- [x] **K7: the dependency and the tightened contract** · layer 7 boundary (config) · *S* — **Done 2026-10-01.** `tabpfn-client` 0.6.1 in the `prediction` extra; contract 3 forbids `tabpfn_client` outside execution (line added by the owner: the `.importlinter` hook blocks agent edits); 4 tests in `test_layer_boundaries.py` incl. probe-import runs of the real contracts. Costs found: `pandas` 3 to 2.3.3, and a PostHog telemetry path (see ADR-0012 4a; K9 must disable it).
  - Acceptance: `tabpfn-client` in the `prediction` extra (`pyproject.toml`, `uv.lock`);
    mypy override scoped to `tabpfn_client.*` (config entry, no inline suppression);
    `.importlinter` contract 3 gains `tabpfn_client` (a tightening). The client's
    installed docs are read and the verified surface (token, fit/predict, limits) is noted
    in ADR-0012. `osv-scanner` run locally if installed.
  - Verify: `uv run lint-imports` green; extend `tests/fitness/test_layer_boundaries.py`
    (a probe import of `tabpfn_client` from `prediction`/`context` fails, from `execution`
    is allowed); `stack_guard.py --base main` reports a tightening, not a weakening.
  - Depends: none. Files: `pyproject.toml`, `uv.lock`, `.importlinter`, test, ADR (~5).

- [x] **K8: `HostedTabPFNScorer` against a fake client** · layers 7 + 4b (Protocol) · *M* — **Done 2026-10-01.** `execution/hosted_scorer.py`; `engine._encode` to `encode`; 17 tests in `test_prediction_gate.py` (fake client: out-of-fold, same folds as local, version recorded/refused/moved, wrong host refused before any fit, failure not retried, default seams without a call). Open: `billing_model_version` as the served checkpoint is unverified until K12; upload deletion left to Checkpoint C (ADR-0012 §5).
  - Acceptance: `execution/hosted_scorer.py` implements `ChurnScorer`; reuses
    `fold_assignment` and a **public** `encode` (promoted from `engine._encode`, behaviour
    unchanged); the client is a seam (as `build_classifier`); the PriorLabs host is a
    checked constant; the served model version is recorded in `ChurnScores.model_version`
    and a scorer that cannot determine it **refuses**; a transport/quota failure is a
    `ScoringError`, never retried silently and never degraded to a guess.
  - Verify: extend `tests/fitness/test_prediction_gate.py`: a fake client recording which
    rows each call was fit on proves **no row is predicted by a model that saw its label**
    (same shape as the local scorer's test); unrecordable version refused; wrong host
    refused; `uv run lint-imports` + `test_capability_is_not_execution.py` stay green.
  - Depends: K7. Files: `hosted_scorer.py`, `engine.py` (rename only), `churn.py`?, test (~4).

- [x] **K9: hosted preflight and startup wiring** · layers 4b + 1 · *S* — **Done 2026-10-02.** `HostedTabPFNScorer.preflight()` (token, host, four synthetic rows, served version required; failure is `LicenceRefused`); `preflight --scorer hosted`; `worker --scores hosted` preflights that variant; client telemetry forced off before import; `record_scores.py` reads `.env`. **Contract 3 amended, owner-approved:** `ignore_imports` for exactly the two CLI edges to `hosted_scorer`, pinned by a test and recorded in ADR-0012. 13 tests added.
  - Acceptance: a hosted preflight (one tiny fit/predict) behind the existing licence gate;
    `worker_cli` accepts a `hosted` scorer kind and `preflight_cli` runs the hosted
    variant; `check_token` still runs first; the scripts that need `TABPFN_TOKEN` load `.env`.
  - Verify: extend `tests/fitness/test_prediction_gate.py` (missing/short token refused at
    startup for the hosted kind, before any call; preflight failure → `LicenceRefused`);
    `uv run pytest tests/fitness/test_layer_boundaries.py` green.
  - Depends: K8. Files: `licence.py`/`hosted_scorer.py`, `worker_cli.py`, `preflight_cli.py`, test (~4).

- [x] **K10: evidence for the hosted call** · layer 9 · *S* — **Done 2026-10-02.** `runtime.operator.TracedScorer` wraps the cycle's scorer per call and leaves one `execution.read` span (`tool=churn.score`, dataset, rows, folds, served `model_version`, `latency_ms`, `outcome`; a failure records `error` as the exception type, never its message). Wired in `evaluate_cycle`; no span type added (still 9), no audit record (a read is traced, not audited). 5 tests in `test_hosted_call_evidence.py`, including that no feature value, column name or token reaches a span. One span per `score()`, not per fold fit: the fold fits are one call to the cohort's owner. README fitness count 49 to 50.
  - Acceptance: each hosted call emits a trace/audit record with rows, folds, served
    version, latency and outcome, using an **existing** span type (no type removed; none
    added unless none fits, in which case the count bump is stated); **never** row contents
    or the token.
  - Verify: `uv run pytest tests/fitness/test_trace_completeness.py tests/fitness/test_audit_separate_from_traces.py`
    plus a new assertion that a recorded call carries no feature values or token.
  - Depends: K8. Files: scorer/observability glue, tests (~3).

### Checkpoint B: egress path built, nothing sent (KKBox will not use it, 2026-10-02)
- [ ] `uv run pytest tests/fitness`, `lint-imports`, `stack_guard.py`, `check_task.sh` green
- [ ] `tabpfn_client` imported only in `agentstack.execution` (K7/K8 proof)
- [ ] ADR-0012: placement, egress bound, read-not-effect, version provenance written
- [ ] You re-read the ADR-0004 question (does a fixed host constant satisfy "a real client
      arrives with its containment dimension"?). If not, a `Sandbox` field is added here.
- [ ] Human review

---

## Phase C: real data and real scores (needs you)

- [x] **K11a: fetch the raw files and explore what they could predict** · scripts (read-only) · *M*
  - Acceptance: `fetch_datasets.py` downloads the competition files (`kaggle competitions
    download -c kkbox-churn-prediction-challenge`) into `data/` (gitignored); a local,
    read-only `scripts/explore_kkbox.py` reports date ranges (to fix the cutoff), label
    balance, and for each **candidate target** (`is_churn`; spend in the N days after the
    cutoff; plan up/downgrade; auto-renew switched off; time-to-churn) its definition, base
    rate or distribution, how it relates to the others, and whether it is separable from the
    features without leakage. Output: `docs/evidence/kkbox-exploration.md`, **aggregates only**
    (no user-level rows, no `msno`). No hosted call, no egress. It ends with a recommendation
    per target: ship / defer / reject, with the layer cost (a non-binary target needs a new
    4b Protocol; predicted revenue can never be the value-at-risk floor).
  - Verify: new tests in `tests/fitness/test_kkbox_mapping.py` on the fixture: each candidate
    target is derived from **post-cutoff** transactions only and shares no row with the
    feature window; the script lives outside the package (`test_layer_boundaries.py` stays
    green); locally, `uv run pytest tests/live/test_kkbox_exploration.py` re-derives the
    report's headline numbers from the real files and they match the committed report
    (reproducible, seeded).
  - Depends: none (Kaggle rules already accepted per `kaggle competitions list`). Fetches
    only `train_v2.csv`, `transactions_v2.csv`, `members_v3.csv`; never `user_logs*`.
    Files: `fetch_datasets.py`, `explore_kkbox.py`, `docs/evidence/kkbox-exploration.md`,
    fixture + test (~5).

  - **Done 2026-10-01.** `fetch_datasets.py --kkbox` (idempotent; fetches five files, never
    `user_logs*`); `scripts/explore_kkbox.py` writes `docs/evidence/kkbox-exploration.md`
    (computed, aggregates only) and the hand-written `kkbox-exploration-reading.md`;
    `tests/fitness/test_kkbox_mapping.py` (8 tests, fixture-only) proves targets look forward
    only, features look back only, the organisers' gap algorithm, and tie ordering;
    `tests/live/test_kkbox_exploration.py` regenerates the report from the real files and it
    matches byte for byte (passed locally, ~6 min). README fitness count 48 → 49.
  - **Findings that change the spec** (see the reading): (1) `transactions_v2` is mostly the
    label window, so history needs v1 `transactions.csv`; (2) cutoff is **2017-02-28**, fixed by
    the data, not chosen; (3) population is `train_v2` as given (my first re-derivation dropped
    users who churn at 65.8%); (4) only `is_churn` is viable: revenue is ≈ last price ×
    not-churned (97.0% of renewers pay their last amount), time-to-churn is censored/degenerate,
    plan moves are 2.5%.

### Checkpoint E: what the data can predict (decision point) — **decided 2026-10-01**
- [x] You read the exploration and chose: ship `is_churn` only; defer spend and plan-move;
      reject time-to-churn and cancel. No target beyond `is_churn`, so no re-plan
- [x] Cutoff **2017-02-28** and population **`train_v2` as given** accepted
- [x] Spec amended: v1 `transactions.csv` is a raw input
- [x] Human review

- [x] **K11: build the cohort file, declared cut** · scripts + layer 5 · *M*
  - Acceptance: `scripts/build_kkbox_cohort.py` builds
    `data/kkbox-cohort.csv` via `kkbox.py` (offline, outside the package); a pure
    `sample_users` (stratified on the label, seed-pinned, deterministic) draws the cut;
    **the cutoff date is the one K11a recommended and Checkpoint E accepted, recorded in ADR-0012**;
    `fetch_datasets.py --record` writes the manifest entry.
  - Verify: new fitness test for `sample_users` (deterministic, stratified, same seed →
    same users; never widened to a target) in `test_kkbox_mapping.py`; then locally
    `uv run pytest tests/live/test_real_cohorts.py` (cohort matches manifest).
  - Depends: K11a + Checkpoint E. Files: `build_kkbox_cohort.py`, `kkbox.py`, test, manifest (~4).
  - **Done 2026-10-02.** `kkbox.sample_users` (pure, seeded, stratified, row-order independent, rare
    class keeps >= 1) plus `COHORT_USERS = 50,000` (provisional until K12) and `COHORT_SEED`;
    `scripts/build_kkbox_cohort.py` (~30 s) wrote 49,863 rows, churn 8.9%; manifest entry recorded;
    `tests/live/test_real_cohorts.py` green for all three cohorts. 6 tests added (41 in the file).

### Checkpoint C: before the first long local scoring run
- [x] Third-party processing: **decided no** (2026-10-02, ADR-0012 §6). No rows leave the machine
- [x] `TABPFN_TOKEN` is set in `.env`; local `preflight` loads the weights (2026-10-02: `tabpfn-3.5 loaded`, 5 s)
- [x] The machine can hold the run: 113 GiB disk free, 18 GiB RAM, 11 cores (device: see K12); RAM for the fold size K12 measures
- [ ] Human review

- [x] **K12: time one local fold, then size the cut** · layer 4b · *S* (rewritten 2026-10-02)
  - Acceptance: `scripts/` timing run (no committed output) fits `TabPFNScorer` on one fold at
    two or three sample sizes of the real cohort and records fit+predict seconds per fold and
    peak memory in ADR-0012, with the device used (CPU / MPS / CUDA); the cut size in
    `build_kkbox_cohort.py` is set so the full 5-fold run fits a budget you name (e.g. one
    overnight run); if it does not, the **cut shrinks, the folds do not**. The cohort is
    rebuilt at the new size and its `data_as_of` re-recorded.
  - Verify: `uv run pytest tests/live/test_real_scores.py::test_preflight_loads_the_weights`
    passes; ADR section populated; K11's `sample_users` test and `tests/live/test_real_cohorts.py`
    green at the new size.
  - Depends: K11, CP-C (K9/K10 no longer gate it). Files: ADR, build script constant, timing script (~3).
  - **Done 2026-10-02.** `scripts/time_kkbox_fold.py`; full 49,863-row fold 502 s, so 5 folds ≈ 42 min,
    peak 2.2 GiB (MPS). Cut stays 50,000, no rebuild, manifest unchanged. Table in ADR-0012 §6.

- [x] **K13: the raw-date comparison, once** · layer 5 + scripts · *S*
  - Acceptance: a single declared out-of-fold comparison, with vs without the raw
    registration/expiry date candidates, on a small sample; the winner and both numbers go
    in ADR-0012; a candidate is kept only if known pre-cutoff, not an identifier, and
    |corr| ≤ 0.9. Not repeated until one wins.
  - Verify: `uv run pytest tests/live/test_real_cohorts.py` (no feature |corr| > 0.9);
    `tests/fitness/test_data_snapshot.py` (drops still all have reasons).
  - Depends: K12. Files: `datasets.py`/`kkbox.py`, ADR, manifest (~4).
  - **Done 2026-10-02.** `kkbox.candidate_refusals` (the three rules, pure) with 5 tests; `scripts/compare_kkbox_raw_dates.py`
    (local, 5,000 users, declared tie band). **Without wins:** log loss 0.17008 vs 0.17068 with `expiry_date_raw`;
    `registration_date_raw` was refused as an identifier at that sample size. Cohort and manifest unchanged. ADR-0012 3c.

- [ ] **K14: record the KKBox scores (local only)** · scripts + layer 4b · *M* (rewritten 2026-10-02)
  - Acceptance: `record_scores.py --datasets kkbox-churn` uses the local `TabPFNScorer` (as it
    already does; no scorer switch is added, so the hosted path is not reachable from this
    script) and records `data/scores/kkbox-churn.json`, **not committed**; a fitness test asserts
    the file path is git-ignored; the script's docstring stops saying every output is committed.
    The run is long: it prints per-fold progress and does not write a partial file.
  - Verify: `uv run pytest tests/live/test_real_scores.py` (recorded scores match the real
    model; scores separate churners; out-of-fold log loss beats the prior; reproducible);
    new `tests/fitness` assertion `git check-ignore data/scores/kkbox-churn.json`.
  - Depends: K13. Files: `record_scores.py`, tests (~3).
  - Note: `test_the_recorded_scores_are_still_what_the_model_produces` re-scores in the live lane,
    so at the K12 cut it is as slow as the recording. If that is too slow for a routine local
    run, it scores a fixed subsample, declared in the test, and never a widened one.

### Checkpoint D: before the cohort is used in an experiment
- [ ] `uv run pytest tests/live` green locally (cohort, correlation bar, scores)
- [ ] `git status` shows no score, raw data or token staged
- [ ] Human review

---

## Phase D: end to end and close-out

- [ ] **K15: trigger → candidate → approval on `kkbox-churn`** · layers 1, 3, 5 (no change to 8) · *M*
  - Acceptance: **CI variant:** a tiny fixture cohort + deterministic fake scorer takes a
    trigger to an approval prompt that names a headcount and an NTD value at risk, and
    approval still sits immediately before the rollout, bound to run id, fingerprint and
    snapshot. **Local variant:** the worker with `RecordedScorers` and the real recorded
    file does the same on the real cohort.
  - Verify: extend `tests/fitness/test_trigger_to_candidate.py` and
    `tests/fitness/test_approve_resume_rollout.py` (CI); local smoke run; `uv run pytest
    tests/durability` and `uv run python -m evals run --gates` green.
  - Depends: K3, K6, K14. Files: tests, fixture glue (~3).

- [ ] **K16: close-out** · docs + guards · *S*
  - Acceptance: ADR-0012 complete (all nine topics, incl. the owner's competition-rules
    statement); `CONSTRAINTS.md`/README counts updated **only to the truth** (fitness test
    count, the lines CI cannot cover, the "not yet recorded" paragraph), each such edit
    flagged to you; `/review` then `/stack-audit` (mandatory) then `/ship`.
  - Verify: `bash scripts/check_full.sh`, `uv run pytest tests/fitness`,
    `uv run python scripts/stack_guard.py --base main`,
    `uv run python scripts/checkpoint_guard.py --base main`, `uv run python scripts/replay_guard.py`,
    README-claims test in `tests/fitness`, `osv-scanner scan source -r .` (CI).
  - Depends: K15. Files: docs, `CONSTRAINTS.md`, README (~4).

### Checkpoint: complete
- [ ] All 8 success criteria in `SPEC-kkbox-cohort.md` met or explicitly deferred with a reason
- [ ] `/stack-audit` findings addressed; the two named debts (PII in traces, secrets in env) still named
- [ ] Human review
