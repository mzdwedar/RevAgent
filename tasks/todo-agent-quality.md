# Tasks: model evals, bounded discretion, and the learning loop

Plan: `tasks/plan-agent-quality.md`. Separate from `tasks/todo.md` and
`tasks/todo-kkbox-cohort.md` (both have open items).

Standing bar on every task (`CONSTRAINTS.md`): `check_fast.sh` per edit, `check_task.sh` at
turn end, changed-line coverage ≥ 80%, no weakened bar (`stack_guard`), no suppression
comments. Each task names its **layer** and its **proving test**. Sizes: XS/S/M (nothing
over ~5 files). Run suites against the isolated Postgres on 5434 while other sessions are
active.

**Decided 2026-10-05** (plan §Decisions):
- local Ollama engines only, and a local grader;
- a single terminal analysis;
- semi-synthetic outcomes;
- promote / stop / extend on the INSV 95% interval;
- include bounded discretion (Part D).

**Before D0:** sign off the percentage range and the offer catalog (plan §Open questions 1–2).

**Preconditions (yours, not tasks):**
- Ollama running with `qwen3:8b`.
- `TABPFN_TOKEN` in `.env` (L3).
- About 1¼ hours to label drafts (E5: 40 × 6).

---

## Part E: evals that measure the model

### Phase E1: measure the model you have

- [x] **E1: behaviour-case format and runner** · layer 9 · *M*
  - Acceptance:
    - `evals/behaviour/*.json` cases give a trigger, frozen cohort, stage and context. They
      contain no proposal. Expectations: tool chosen, admission passes (no
      `draft_deviation` / `rollout_deviation`), required draft fields present and non-empty.
    - `python -m evals behave --engine echo` runs them through the real stack
      (`run_turn`, real registry and admission). It reuses `_stack_for`; nothing in `runner.py`
      for gates changes behaviour.
    - ~10 cases to start: 6 drafting (different cohorts, currencies, risk bands), 2
      rollout, 2 with injected text in context.
  - Verify: `tests/fitness/test_behaviour_cases.py`: a behaviour case containing a scripted
    proposal is rejected at load. A gate case cannot be loaded as a behaviour case. With a
    scripted engine, a known-good and a known-bad proposal grade pass/fail as expected.
    `uv run python -m evals run --gates` unchanged (20/20).
  - Depends: none. Files: `evals/behaviour.py`, `evals/__main__.py`, `evals/behaviour/*.json`, test.

- [x] **E2: k trials per case, pass@1 and pass^k** · layer 9 (+ layer 4 engine factory) · *S*
  - Acceptance:
    - `--engine ollama:qwen3:8b --k 5` runs each case k times with seeds 0..k-1 (temperature
      stays as configured).
    - Each trial appends one JSONL row: case, engine, model asset name, seed, prompt hash,
      passed, failure reasons, latency.
    - The summary prints pass@1 (mean) and pass^k (all-k) per case and overall, with a
      Wilson 95% interval on pass@1.
    - Trials already in the JSONL for the same (case, engine, seed, prompt hash) are skipped.
  - Verify: unit tests for the pure aggregation (`pass_hat_k`, Wilson interval) against
    hand-computed values. Live smoke test in `tests/live/test_behaviour_live.py` (k=1, one case).
  - Depends: E1. Files: `evals/behaviour.py`, `evals/stats.py`, live test.

- [x] **E3: proposal-refusal rate per model** · layer 9 · *S*
  - Acceptance:
    - Per trial, classify the outcome from the audit trail and turn result as one of:
      admitted, `proposal.deviates` (which field differed), malformed (validation), wrong
      tool, no proposal.
    - The summary reports the distribution per engine.
  - Verify: test with a scripted engine producing each class once, asserting the
    classification reads the audit rows (not the model text).
  - Depends: E2. Files: `evals/behaviour.py`, test.

#### Checkpoint E-A (the baseline)
- [x] qwen3:8b × 10 cases × k=5 run on the *narrow* agent (temperature 0.7). pass@1, pass^k and the refusal distribution recorded in `docs/evidence/behaviour_qwen3_8b_narrow.jsonl`
- [x] Gates 20/20, `check_task.sh` green, `stack_guard` clean
- [ ] Review the failure reasons with you before widening anything

---

## Part D: bounded discretion

The model chooses the offer and the rollout percentage. Policy holds the bounds, admission
refuses anything outside them, and the approval binds the exact values chosen.

- [ ] **D0: ADR and constraint rows for bounded discretion** · docs + layer 8 · *S*
  - Acceptance:
    - ADR-0015 records:
      - which arguments are record-held and which discretionary;
      - where the bounds live (`experiments/offers.toml`, layer 8);
      - that bounds never come from context;
      - the percentage range and offer catalog you signed off (plan §Open questions 1–2).
    - `CONSTRAINTS.md` gains rows and loses none:
      - every discretionary argument has a bound in config;
      - an out-of-bounds proposal never reaches a person;
      - the approval prompt shows each chosen value with its allowed range.
    - `SPEC.md` iteration-1 decisions say the percentage is chosen within the range,
      superseding "~10%".
  - Verify: `uv run python scripts/stack_guard.py --base main` passes (rows added, none
    weakened). `/stack-audit` on the ADR.
  - Depends: Checkpoint E-A, your sign-off. Files: `docs/adr/0015-*.md`, `CONSTRAINTS.md`, `SPEC.md` (own hunks).

- [ ] **D1: offer catalog and bounds as policy** · layer 8 · *S*
  - Acceptance:
    - `experiments/offers.toml` defines, per profile:
      - offer kinds with min/max amount and duration;
      - the percentage range;
      - the minimum treatment arm (100);
      - the total-offer-cost cap as a fraction of value at risk.
    - `policy/offers.py` loads it into frozen `Bounds`. `offer_cost(kind, amount, observed_arpu)`
      is a pure function. Unknown kinds and missing currencies are refused, never defaulted.
    - Each bound check returns a reason string, so admission can quote it.
  - Verify: `tests/fitness/test_offer_bounds.py`:
    - bounds are loaded only from config (no context input in the signature);
    - cost is deterministic, in the cohort's currency, and from observed ARPU;
    - an unknown kind is refused.
  - Depends: D0. Files: `experiments/offers.toml`, `policy/offers.py`, test.

- [ ] **D2: the model chooses the offer** · layers 6 + 3 · *M*
  - Acceptance:
    - `create_experiment_draft` gains scalar `offer_kind` (enum from the catalog),
      `offer_amount` (int) and `offer_months` (int). `variant` remains as the model's
      free-text label.
    - The draft instruction states the catalog and caps. The model picks.
    - `draft_deviation` keeps the version and resource checks. It also refuses an offer
      out of bounds, or one whose total cost over the cohort's treatment arm exceeds the
      cap. That refusal is audited as `proposal.out_of_bounds`.
    - The draft stores the computed cost per customer and the total, as first-party.
  - Verify:
    - `tests/fitness/test_registry_tools.py` still passes: no array or object args, the
      draft menu is still 5.
    - `tests/test_draft_deviation.py` covers in-bounds, over-cap and unknown-kind proposals.
    - New gates: `an-offer-over-the-cap-never-reaches-a-person` and
      `injected-text-cannot-raise-an-offer-cap`.
  - Depends: D1. Files: `tools/experiments.py`, `runtime/drafting.py`, tests, 2 eval cases.

- [ ] **D3: the model chooses the rollout percentage** · layers 3 + 8 · *M*
  - Acceptance:
    - `rollout_instruction` still dictates the record-held arguments "exactly as given".
      For `percentage` it states the allowed range and the minimum arm in customers,
      and asks for a one-line reason in the turn text.
    - `rollout_deviation` holds the record-held fields to the record and the percentage
      to the bounds. The estimated arm must be ≥ the minimum.
    - The approval fingerprint binds the chosen percentage. What is approved is what commits.
  - Verify:
    - New gates: `a-percentage-outside-the-range-never-reaches-a-person`,
      `the-approved-percentage-is-the-one-rolled-out`, and
      `injected-text-cannot-widen-the-range`.
    - Existing `rollout-commits-exactly-once` and `rollout-requires-approval` still green.
    - `tests/durability/test_approve_after_death.py` still green.
    - `replay_guard.py` and `checkpoint_guard.py --base main` green.
  - Depends: D1. Files: `runtime/drafting.py`, `policy/offers.py`, 3 eval cases, tests.

- [ ] **D4: the approver sees the choices** · layer 1 · *S*
  - Acceptance:
    - The Slack approval shows:
      - the offer, its cost per customer and its total cost for the headcount;
      - the chosen percentage *and* the allowed range, as a headcount (row 133 holds);
      - the model's reason, quoted and labelled "model-written".
    - The footer is unchanged (run id, wait id, expiry).
  - Verify: `tests/fitness/test_slack_outbound.py` is extended. An approval without range or
    cost fails. The model's reason renders escaped, so it cannot add buttons or links.
    Re-take `docs/evidence/slack_approval.png`.
  - Depends: D2, D3. Files: `interfaces/slack.py`, test, evidence image.

- [ ] **D5: decision behaviour cases, before and after** · layer 9 · *S*
  - Acceptance:
    - Behaviour cases gain decision expectations:
      - in bounds;
      - minimum arm met;
      - offer cost under the cap;
      - a cheaper offer when ARPU is low (2 cases where the right direction is clear).
    - qwen3:8b re-runs k=5.
    - A before/after table compares the narrow and wide agent: pass@1, pass^k, refusal mix
      (now including `proposal.out_of_bounds`), and the spread of chosen offers and
      percentages.
  - Verify: aggregation test on fixture JSONL. Results in `docs/evidence/behaviour_qwen3_8b_wide.jsonl`.
  - Depends: D4, E3. Files: `evals/behaviour/*.json`, `evals/report.py`, test.

#### Checkpoint D
- [ ] Gates 20 + 5 new, all green. `stack_guard` clean (rows added, none weakened)
- [ ] Before/after table reviewed with you: did widening cost reliability? Is the refusal rate acceptable?
- [ ] `/stack-audit` clean

### Phase E2: compare models, grade drafts

- [ ] **E4: token usage on the contract; model matrix** · layer 4 · *S*
  - Acceptance:
    - `ModelResponse.usage` is optional (prompt and output tokens) and filled in by
      `OllamaEngine` from `prompt_eval_count` / `eval_count`. `EchoEngine` leaves it None.
    - Behaviour runs record tokens and latency.
    - The matrix runs at least 3 engines (per Open question 1).
  - Verify: `tests/fitness/test_model_contract.py` (or existing contract test) extended: usage is
    optional, and no layer above 4 reads Ollama-specific fields. `.importlinter` unchanged.
  - Depends: E2, D5. Files: `model/contract.py`, `model/ollama_engine.py`, `evals/behaviour.py`, test.

- [ ] **E5: draft rubric and labelling set** · layer 9 · *S* (+ ~1h of your labelling)
  - Acceptance:
    - `evals/rubrics/draft.md` defines 6 yes/no criteria, each with a pass and a fail example:
      - **C1 falsifiable**: the hypothesis names a measurable outcome and a direction.
      - **C2 concrete variant**: the variant is a specific, deployable offer.
      - **C3 fits the cohort**: the draft is consistent with the evidence given (risk band,
        plan, currency).
      - **C4 no invented facts**: every number or claim traces to the context.
      - **C5 cost-aware**: the draft acknowledges what the offer gives away.
      - **C6 choice justified**: the offer and percentage are argued from the cohort's
        evidence, not picked arbitrarily.
    - `python -m evals sample-drafts --n 36` draws drafts across engines from the E2/E4 JSONL
      into `evals/labels/drafts.jsonl`, plus 4 drafts that try to inject a grader (40 in all).
    - The order is shuffled and the engine name is hidden from the labeller.
    - The owner labels every criterion (40 × 6 = 240 yes/no). An optional note is allowed
      when a call is borderline. "Can't tell" is allowed and excluded from κ.
  - Verify: test that the sampler is stratified by engine and deterministic for a seed.
    Labels file validates against the rubric's criteria.
  - Depends: E4. Files: `evals/rubrics/draft.md`, `evals/labelling.py`, test.

- [ ] **E6: rubric grader, calibrated per criterion** · layer 9 · *M*
  - Acceptance:
    - The grader takes (rubric, draft, cohort evidence) and returns a verdict and a one-line
      reason per criterion, as structured output validated against a schema. The draft is
      wrapped as data.
    - `python -m evals calibrate` reports Cohen's κ per criterion against the E5 labels.
      Criteria with κ < 0.6 are marked human-only and excluded from grader scores.
    - The 4 injection drafts must not be graded better than their labels.
  - Verify: unit test for κ against a hand-computed table. Test with a scripted grader:
    a malformed grader response becomes "ungraded", never "pass". Live calibration result
    committed to `docs/evidence/grader_calibration.json`.
  - Depends: E5. Files: `evals/grader.py`, `evals/stats.py`, tests.

- [ ] **E7: evidence report** · layer 9 / docs · *S*
  - Acceptance:
    - `python -m evals report` writes `docs/evidence/model_evals.csv` and a chart: per engine,
      pass@1 ± CI, pass^k, refusal mix, grader score per calibrated criterion, median tokens
      and latency.
    - The README gets a short "Evals that measure the model" section next to the gates
      section, including what is *not* claimed.
  - Verify: report test on a fixture JSONL (golden CSV). README links resolve.
  - Depends: E6. Files: `evals/report.py`, `README.md` (own hunk only), test.

#### Checkpoint E-B
- [ ] Matrix of ≥3 engines in `docs/evidence/model_evals.csv`
- [ ] Grader κ reported per criterion. Low-κ criteria shown as human-only
- [ ] Review with you: does any result change which model the operator should run on?

---

## Part L: the learning loop (iteration 2)

### Phase L1: prove the estimator

- [ ] **L0: spec amendment and ADR** · docs · *S*
  - Acceptance:
    - `SPEC-learning-loop.md` has the three Agent Stack sections (Layer Ownership Ledger,
      Foundation Assumptions, Boundary Decisions) and records Open questions 3–5 as decided.
      It also covers the outcome seam, the memory trust split, and the readout's success
      criteria (including SPEC criterion 12: INSV reads only observed quantities).
    - ADR-0014 records semi-synthetic outcomes: what is real, what is planted, and where the
      label must appear.
  - Verify: `/stack-audit` on the spec. `layer_of.py` prints a ledger row for every new module.
  - Depends: Open questions 3, 4, 5. Files: `SPEC-learning-loop.md`, `docs/adr/0014-*.md`.

- [ ] **L1: randomise within the frozen cohort** · layer 5 · *S*
  - Acceptance:
    - `assign(experiment_version, subscriber_id) -> arm` is deterministic (hash-based) and
      splits the treatment arm at the rollout percentage.
    - The control arm is drawn only from the same frozen cohort. This is the
      regression-to-the-mean Never from SPEC.md.
  - Verify: `tests/fitness/test_assignment_within_cohort.py`:
    - The control arm is a subset of the cohort.
    - Assignment is stable across processes.
    - The split is within ±2pp of the target at n = 1,000.
    - A subscriber outside the cohort has no arm.
  - Depends: L0. Files: `context/assignment.py`, test.

- [ ] **L2: outcomes seam and semi-synthetic simulator** · layer 5 + `scripts/` · *M*
  - Acceptance:
    - `context/outcomes.py` loads post-rollout outcomes per subscriber (renewed, revenue in
      window, offer cost) with provenance, including `simulated`.
    - Rows dated before the rollout are refused, as are rows for subscribers outside the cohort.
    - `scripts/simulate_outcomes.py` takes a KKBox frozen cohort and a draft's offer.
      Control outcomes are the real post-cutoff renewals. The treatment arm gets a planted
      heterogeneous effect (persuadables only) whose size depends on the offer kind and
      amount, plus the offer's computed cost (D1).
    - The effect table is set so INSV ranks the offers. At least one offer must lose money,
      so there is something to learn.
    - The script writes the true τ and the true INSV alongside the outcomes.
  - Verify: `tests/fitness/test_outcomes_seam.py` (leak refusal, provenance carried). The simulator test checks
    that with τ = 0 the arms' outcome distributions match.
  - Depends: L1, D1 (offer kinds and cost). Files: `context/outcomes.py`, `scripts/simulate_outcomes.py`, tests.

- [ ] **L3: `eLTV_pre` covariate** · layer 4b · *M*
  - Acceptance:
    - TabPFN regressor `ŷ_spend` and classifier `ŷ_conv` are computed on pre-treatment features
      at freeze time, combined into `eLTV_pre`, and recorded like `RecordedScorer`
      (`data/scores/`), so CI needs no token.
    - Model version is pinned and carried in provenance.
  - Verify: `tests/fitness/test_covariate_is_pre_treatment.py`:
    - The covariate's `data_as_of` ≤ the cohort's.
    - The recorded file round-trips.
    - The checkpoint constant is pinned (extend `test_prediction_gate.py`).
  - Depends: L0. Files: `prediction/engine.py`, `prediction/covariate.py`, `scripts/record_scores.py`, tests.

- [ ] **L4: ANCOVA and INSV readout** · layer per `layer_of.py` (expected 5) · *M*
  - Acceptance:
    - A pure function `(outcomes, assignment, covariate) -> Readout` computes τ̂ by OLS
      `outcome ~ treatment + eLTV_pre` with HC2 standard errors, plus:
      - the unadjusted τ̂ for comparison;
      - INSV = (saved LTV_t − saved LTV_c) − offer cost (the draft's stored D1 cost, observed), with a CI;
      - the covariate's realised correlation and the variance-reduction ratio;
      - the `simulated` flag.
    - No predicted quantity enters the INSV outcome columns.
  - Verify: `tests/fitness/test_readout.py` over 100 simulated seeds:
    - The 95% CI covers the planted τ in ≥ 92 seeds.
    - With τ = 0 the false-positive rate is ≤ 8%.
    - The adjusted CI is no wider than the unadjusted one on average.
    - The INSV input schema contains only observed columns (criterion 12).
  - Depends: L1, L2, L3. Files: `context/readout.py`, test (+ `statsmodels` oracle test only if approved).

#### Checkpoint L-A
- [ ] Coverage, false-positive rate and variance reduction committed to `docs/evidence/readout_simulation.csv`
- [ ] Review with you: are the planted effects and thresholds credible?

### Phase L2: close the loop

- [ ] **L5: decision policy and a durable conclude step** · layers 8 + 3 · *M*
  - Acceptance:
    - Policy maps a Readout to promote / stop / extend using thresholds from config (Open question 5).
    - The workflow gains a conclude step at the pre-declared endpoint (a durable timer).
      The readout runs as an activity. Promote goes through the existing approval path.
      Stop uses `HALT`.
    - The LLM writes the Slack explanation only, and the message names `simulated`.
  - Verify:
    - `tests/durability/test_conclude.py`: kill the worker mid-readout, resume, exactly one
      conclusion.
    - New gates `a-negative-insv-never-promotes` and `a-promotion-still-requires-approval`.
    - `replay_guard.py` and `checkpoint_guard.py --base main` green (workflow change is patched).
  - Depends: L4. Files: `policy/conclusions.py`, `runtime/temporal/workflows.py`, `runtime/temporal/activities.py`, 2 eval cases, test.

- [ ] **L6: conclusion written to memory, split by trust** · layer 5 · *S*
  - Acceptance:
    - On conclusion, `memory.write()` is called once with first-party fields:
      - cohort description, offer kind, amount and months, cost per customer, chosen percentage;
      - τ̂, CI, INSV, covariate correlation;
      - `simulated`, `experiment_version`.

      Provenance is the readout id, and the record has a TTL.
    - The model-written hypothesis is written as a separate `UNTRUSTED` item.
    - Nothing else writes memory.
  - Verify: `tests/fitness/test_learning_memory.py`:
    - First-party numbers come only from the Readout.
    - A conclusion replayed twice writes once.
    - There is no write path from model output (extend the existing "no implicit memory
      writes" fitness test if there is one).
  - Depends: L5. Files: `context/learnings.py`, `runtime/temporal/activities.py`, test.

- [ ] **L7: prior learnings in the drafting context** · layer 5 · *M*
  - Acceptance:
    - Next to the existing `cohort_profile()`, the drafting turn's context includes up to 3
      prior learnings for the same tenant. They are ranked deterministically by cohort
      similarity, and each carries scope, provenance, freshness, reason and trust.
    - Each learning states the offer tried, its cost and its INSV with CI.
    - Untrusted hypotheses render quoted.
  - Verify: new gates `a-concluded-experiment-reaches-the-next-draft`,
    `another-tenants-learning-is-never-recalled`, and
    `an-injected-hypothesis-in-memory-does-not-widen-the-menu`. `draft_deviation` behaviour
    unchanged. Prompts stay out of Temporal history (existing boundary test).
  - Depends: L6. Files: `context/assemble.py`, `context/learnings.py`, `runtime/drafting.py`, 3 eval cases.

#### Checkpoint L-B
- [ ] Gates 23+/23+ green. Durability suite green on the isolated DB
- [ ] `/stack-audit` clean

---

## Phase X: the payoff

- [ ] **L8: two sequential experiments, end to end** · scripts / evidence · *S*
  - Acceptance:
    - `scripts/learning_loop_demo.py` runs experiment A on a simulated KKBox cohort through
      trigger, draft, approval, rollout, outcomes, readout and conclusion.
    - It then runs experiment B. B's drafting context shows A's learning, and the transcript
      is committed to `docs/evidence/learning_loop.txt`.
    - The planted effects make A's offer a loser, so B has a reason to choose differently.
  - Verify: live test `tests/live/test_learning_loop.py` asserts that B's assembled context
    contains A's readout id.
  - Depends: L7.

- [ ] **E8: draft quality with vs without memory, per model** · layer 9 · *S*
  - Acceptance:
    - Behaviour drafting cases are run in two arms, learnings off and on.
    - The report adds, per engine:
      - grader score on the calibrated criteria;
      - refusal rate;
      - "cites prior evidence" (deterministic: the draft names a prior experiment id or result);
      - "avoids the losing offer" (deterministic: with memory, how often the model picks an
        offer kind whose prior INSV was positive, against without memory).
    - The README result states the effect and what is not claimed (simulated outcomes,
      n cases, k trials).
  - Verify: report golden test extended. Results in `docs/evidence/model_evals.csv`.
  - Depends: E7, L8.

#### Checkpoint: complete
- [ ] Every acceptance box above is ticked with evidence committed
- [ ] README: a short "What the model decides" section (D) beside the evals (E) and loop (L) results
- [ ] `check_full.sh` green, `stack_guard` and `checkpoint_guard` clean
- [ ] README updated (own hunks), "simulated" visible wherever Part L numbers appear
- [ ] `/stack-audit` then `/ship`
