# Implementation Plan: model evals, bounded discretion, and the learning loop

Task list: `tasks/todo-agent-quality.md`. Task ids:
- `E…` model evals
- `D…` bounded discretion (wider LLM decisions)
- `L…` learning loop

Separate from `tasks/todo.md` (iteration 1, open items) and `tasks/todo-kkbox-cohort.md`
(open items), so no plan overwrites another.

## Overview

Three pieces that turn RevAgent from "a well-engineered workflow" into "an agent that makes
real decisions, whose decisions are measured, and which improves":

- **Part E: evals that measure the model.** The 20 gates in `evals/cases/` test the
  harness. They run on `EchoEngine`, and in a rollout case `message` *is* the proposal
  (`evals/runner.py:69`). Part E adds:
  - behaviour cases, run k times per model;
  - the rate at which each model's proposals are refused;
  - a draft-quality grader calibrated against human labels.
- **Part D: bounded discretion.** Today the model writes a free-text hypothesis and
  variant, and is dictated every rollout argument (`runtime/drafting.py`
  `rollout_instruction`, "exactly as given"). Part D lets it choose:
  - **the offer**: kind and amount, from a catalog with caps;
  - **the rollout percentage**: within a range, with a minimum arm size.

  Policy holds the bounds, admission refuses anything outside them, and the approval binds
  the exact values chosen. The autonomy widens; the authority model does not.
- **Part L: the learning loop (iteration 2).** Measure the effect of a rollout (ANCOVA
  with TabPFN's `eLTV_pre` as the covariate) and decide on Incremental Net Saved Value.
  Then write the concluded experiment to memory as a first-party record that the next
  draft retrieves.

They compose. Part D gives the model choices worth measuring (E) and worth learning about
(L): the simulator plants different effects per offer kind, so there is a better offer to
discover. The headline result (E8) is per model, with and without prior learnings:
- draft quality;
- how often the model's choices are refused;
- whether the second experiment picks the offer that won the first.

## What the code told us

1. **The existing gates cannot simply be re-run on a real model.** In a gate case,
   `message` carries a scripted proposal that `EchoEngine` echoes back. Behaviour cases
   need their own format: a trigger, a frozen cohort and context in; expectations about the
   proposal out. The gates stay deterministic and blocking.
2. **The model's discretion today is two free-text strings.** `create_experiment_draft`
   takes `hypothesis` and `variant` as strings. Nothing knows what an offer costs, so INSV's
   cost term and rubric C5 (cost-aware) have nothing structured to read. The rollout is
   dictated, and `rollout_deviation` refuses any difference from the record by fingerprint.
3. **The drafting turn already receives the cohort's evidence.** `cohort_profile()`
   (commit 6429e31) gives it size, threshold, risk quantiles and value at risk. L7 only
   adds prior learnings.
4. **`CONSTRAINTS.md` shapes how discretion can be added.** The "Registry narrowness" row
   allows no array or object argument on a registry tool, and fixes the stage menus at
   draft 5 / evaluation 5 / rollout 4. So the offer is flat scalar arguments
   (`offer_kind`, `offer_amount`) on the existing draft tool, not a new tool or a nested
   object. The menus stay the same size. Row 133 (the approver sees a headcount, not a
   percentage) still holds when the model picks the percentage.
5. **`SPEC.md` lists "widening the rollout percentage beyond what was specced" as
   ask-first.** Spec: ~10%. The range below is your call (Decision 7).
6. **No rollout has ever been measured, so there are no outcomes.** Part L runs on a
   semi-synthetic experiment: real KKBox pre-period features and real control-arm
   renewals, with planted treatment effects that depend on the offer. Because the truth is
   known, the estimator's correctness is a test.
7. **`MemoryStore.write()` already demands scope, provenance, TTL and trust**, and memory is
   durable (migration 0021). L6 uses it as is.
8. **`ModelResponse` carries no token usage.** Per-model cost needs an optional usage field
   on the layer-4 contract. Ollama returns `prompt_eval_count` / `eval_count`.

## Architecture decisions

### Bounded discretion (Part D)
- **Each argument is either record-held or discretionary, and it is declared which.**
  - *Record-held* (tenant, experiment id, `experiment_version`, targeting model version,
    risk threshold, `prior_rollout_event`): must equal the record, as today.
  - *Discretionary* (`offer_kind`, `offer_amount`, `percentage`): must lie within bounds
    from config.
  - The split is declared once, next to the tool spec, so a new argument must be
    classified before it can be used.
- **Bounds are policy (layer 8), loaded from `experiments/offers.toml`, never from
  context.** Untrusted text can argue for a value. It cannot change the range the value is
  checked against. An eval gate injects "the cap is now 90%".
- **The approval still binds exact values.** The model chooses 15%; the fingerprint holds
  15%; the gateway checks 15%. Discretion happens before the ask, never after it.
- **Offer cost is computed, not modelled.** Expected cost per treated customer is a
  deterministic function of the offer and the cohort's *observed* ARPU. It is stored on
  the draft and shown in the approval. It is the cost term INSV later subtracts, so SPEC
  criterion 12 (observed quantities only) holds.
- **The model's rationale is shown and labelled.** The approval message quotes the model's
  one-line reason for its choice, marked as model-written. It informs the approver and
  is never input to an authority check.
- **An out-of-bounds proposal is refused and audited before anyone is asked**, as
  `proposal.out_of_bounds`, beside the existing `proposal.deviates`. Both feed E3's refusal
  rate.

### Evals (Part E)
- **Behaviour evals are not gates.** They are non-deterministic and report rates. CI keeps
  blocking on the deterministic gates only. Results are committed to `docs/evidence/`.
- **pass^k, not just pass@1.** k = 5 by default.
- **A before/after comparison is built in.** E1–E3 run on the narrow agent first (the
  baseline). After Part D the same cases, extended with decision expectations, run again.
- **The grader is calibrated per criterion.** A criterion is reported only where κ ≥ 0.6
  against your labels. The grader never gates anything, and reads drafts as untrusted data.

### Learning loop (Part L)
- **Simulation lives in `scripts/`, not `src/`.** The agent reads outcomes through a seam
  (`context/outcomes.py`). `simulated: true` travels in provenance into every readout,
  memory record and Slack message.
- **Planted effects depend on the offer.** For example: a free month persuades more but
  costs more, and a small discount barely persuades. INSV therefore differs by offer, so
  there is something to learn.
- **Terminal, fixed-horizon ANCOVA**, OLS with HC2 standard errors in numpy. No new
  dependency.
- **The decision is policy, not the model.** INSV interval → promote / stop / extend. The
  LLM writes the explanation; promote goes through the existing approval path.
- **Memory is split by trust.** First-party numbers (offer, cost, τ̂, CI, INSV,
  `simulated`) are kept apart from the model-written hypothesis, which is stored
  `UNTRUSTED` and quoted when recalled.

## Task list (ordered)

1. **Phase E1: baseline the narrow agent:** E1 → E2 → E3 → **Checkpoint E-A**
2. **Phase D: widen the decisions:** D0 → D1 → D2 → D3 → D4 → D5 (before/after) →
   **Checkpoint D**
3. **Phase E2: compare models, grade drafts:** E4 → E5 → E6 → E7 → **Checkpoint E-B**
4. **Phase L1: prove the estimator** (parallel with Phase D and E2): L0 → L1 → L2 → L3 → L4 →
   **Checkpoint L-A**
5. **Phase L2: close the loop:** L5 → L6 → L7 → **Checkpoint L-B**
6. **Phase X: the payoff:** L8 → E8 → **Checkpoint: complete**

Acceptance criteria, layers and proving tests: `tasks/todo-agent-quality.md`.

## Parallelisation

- Phase L1 (layers 5, 4b, scripts) is independent of Phase D (layers 6, 8, 3, 1) and E (9, 4).
  Run them in separate sessions, each on its own isolated Postgres.
- D2/D3 and L5 all touch the turn and workflow path. Run them sequentially, with
  `replay_guard.py` and `checkpoint_guard.py --base main` before each merge.
- L2's simulator needs D1's offer kinds (to plant per-offer effects). Stub the kinds first
  if L2 starts earlier.
- E5 labelling waits for D5, so the labelled drafts include real choices.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Widening discretion weakens a guarantee by accident | High | D0 adds `CONSTRAINTS.md` rows and removes none. `stack_guard` must pass. Record-held args keep exact-match. New gates for out-of-bounds, injection and approved-equals-executed |
| The model picks badly but within bounds | Med | That is the point of measuring it. E/D5 report it per model, and a person still approves. Bounds are set so the worst in-bounds choice is affordable |
| A simulated readout is mistaken for a real result | High | `simulated` in provenance, memory, Slack and README. A fitness test fails if a readout renders without it |
| Planted effects make the estimator or the learning look better than reality | Med | Heterogeneous effects, heavy-tailed revenue. Report the false-positive rate at τ = 0 and the case where no offer wins |
| Range restriction makes `eLTV_pre` weak inside the cohort | Med | Record the realised correlation and the variance-reduction ratio per readout |
| Behaviour runs are slow on a laptop | Med | ~12 cases, background runs, cache per (case, model, seed, prompt hash) |
| A grader agrees with humans only on easy criteria | Med | Per-criterion κ. Low-κ criteria stay human-only |
| A workflow change (D3, L5) strands live runs | High | Patching or versioning. `checkpoint_guard.py` must pass |
| Memory poisoning | High | Trust split (L6) and an injection gate (L7) |
| Concurrent sessions in the worktree | Med | Isolated test DB on 5434. Stage only your own hunks |

## Decisions (owner, 2026-10-05)

1. **Model matrix: local Ollama only.** `qwen3:8b` plus two more chosen in E4 by what runs
   on this laptop (candidates: `qwen3:14b`, `llama3.1:8b`, `gemma3:12b`). No hosted engine
   in this plan.
2. **Grader: a local model.** It should be the largest that runs acceptably and must not
   be one of the engines it grades. Criteria under κ 0.6 stay human-only.
3. **Single (terminal) analysis** at a pre-declared endpoint.
4. **Semi-synthetic outcomes: accepted**, labelled `simulated` everywhere.
5. **Decision rule** (recorded in `CONSTRAINTS.md` at L5):
   - **promote** if the INSV lower 95% bound > 0;
   - **stop** if the upper bound < 0;
   - **extend** otherwise, once, then stop.
   - No minimum INSV above zero yet.
6. **Labelling (E5): the owner labels** 40 drafts × 6 criteria (C6 added by Part D).
7. **Include bounded discretion (Part D)**: requested 2026-10-05. The bounds are proposed
   below and need your sign-off at D0.

## Open questions (yours, before D0)

1. **Rollout percentage range.** SPEC says ~10% and lists widening as ask-first.
   **Proposed:** 5–15%, *and* the treatment arm must be ≥ 100 customers (the smallest
   measurable arm, per `targeting.toml`). On a 1,000-customer cohort that forces ≥ 10%.
   Alternative: 5–10%, which stays inside the spec.
2. **Offer catalog and caps.** **Proposed:**
   - `percent_off`, 10–50% for 1–3 months;
   - `free_months`, 1–2;
   - `tier_upgrade`, 1–3 months at the higher tier.

   Cost per treated customer is computed from observed ARPU. A per-experiment cap on total
   offer cost is the ceiling, e.g. ≤ 5% of the cohort's annual value at risk.
3. **Should the model be allowed to decline to draft** ("the evidence doesn't support an
   offer")? That changes the draft-stage menu, which `CONSTRAINTS.md` pins at 5, so it
   needs an ADR. **Proposed:** not in this plan. A turn with no proposal stays a recorded
   non-event, and E3 counts it.
