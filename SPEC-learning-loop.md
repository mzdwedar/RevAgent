# Spec: the learning loop (measure an experiment, remember what it showed)

Status: **rev 1, 2026-10-06.** Companion to `SPEC.md` (rev 10), which is untouched. This file
is named like `SPEC-kkbox-cohort.md` rather than overwriting an approved spec.
Module id: `learning-loop`. Tasks: `tasks/todo-agent-quality.md` Part L (L0–L8).
ADR: `docs/adr/0014-semi-synthetic-outcomes.md`.

---

## Objective

Today the agent can propose an experiment and, after a human approves, roll it out. Nothing is
ever measured, so nothing is ever learned. This spec closes that loop:

```
draft → approve → rollout → outcomes → readout → conclusion → memory → next draft
```

The question stays the one in `SPEC.md` ("did intervening on this targeted cohort save more
than the offers cost?"). What is new:

1. **A readout.** One terminal, fixed-horizon ANCOVA at a pre-declared endpoint gives τ̂ and
   Incremental Net Saved Value (INSV) with a 95% interval.
2. **A decision by policy.** The interval maps to promote / stop / extend. The model never
   decides and only writes the Slack explanation.
3. **A memory of it, split by trust.** The numbers are first-party. The model's own hypothesis
   is stored `UNTRUSTED` and quoted when recalled.
4. **The next draft sees it.** Up to three prior learnings for the same tenant enter the
   drafting context.

**No rollout has ever been measured**, so there are no real outcomes. Part L runs on
*semi-synthetic* outcomes (ADR-0014): the control arm is real KKBox post-cutoff behaviour, the
treatment effect is planted, and the word `simulated` travels with every number. This proves the
estimator and the plumbing. It does not prove any offer works on real customers.

### Not in scope

- Real outcome ingestion from a production system (the seam is built; the source is not).
- Repeated looks and anytime-valid inference. The analysis is terminal (decided below).
- Bounded discretion over percentages and offer sizes (Part D, deferred). The offer menu is the
  five-item `VARIANTS` enum already in `tools/experiments.py`.
- Letting the model decline to draft.
- A new dependency. The regression is OLS with HC2 standard errors in numpy.

---

## Assumptions

1. **Part D is deferred, so L2 does not wait for D1.** The plan had L2 depending on D1 (offer
   kind and cost). Instead the simulator's effect table is keyed on the five `VARIANTS`, each
   with a cost per treated customer defined in `scripts/` and recorded on the draft. If Part D
   returns, the table is re-keyed. The seam does not change.
2. **The treated share is the rollout percentage already on the draft.** `assign()` splits the
   frozen cohort at that percentage. Control is the remainder of the same cohort, nothing from
   outside it.
3. **The frozen KKBox cohort is the only dev data.** Real post-cutoff renewals (March 2017
   label window) are the control outcomes. They are real and they are not a rollout.
4. **Rollouts are not real.** `execution` moves an experiment to `live` in the registry. No
   customer receives anything. "Outcomes" are therefore loaded from a file the simulator writes.

---

## Decisions (Open questions 3–5, recorded as decided)

| # | Question | Decision |
|---|---|---|
| 3 | Terminal or repeated analysis? | **Terminal.** One regression at a pre-declared endpoint. Triggers may accumulate data and run guardrails but cannot decide. Optional stopping disappears. (Plan decision 3.) |
| 4 | What is "Saved LTV" per arm? | **Observed revenue per subscriber in the outcome window**, from the outcomes seam. `Saved LTV_t − Saved LTV_c` is the difference in mean observed revenue times the treated headcount. No predicted spend, churn or LTV enters (SPEC criterion 12). *This is the author's proposal and needs the owner's confirmation before L4.* |
| 5 | Decision thresholds? | **Promote** if the INSV lower 95% bound > 0. **Stop** if the upper bound < 0. **Extend** otherwise, once, then stop. No minimum INSV above zero yet. Thresholds live in config, and the rule is recorded in `CONSTRAINTS.md` at L5. (Plan decision 5.) |

Also fixed: simulated outcomes are accepted and labelled `simulated` everywhere (plan decision 4).

---

## Tech Stack

No new dependency. numpy for OLS and HC2. The covariate (L3) uses the existing TabPFN seam
(`prediction/`), recorded like `RecordedScorer` so CI needs no token.

## Commands

```
python scripts/simulate_outcomes.py --cohort <frozen> --variant <id>    # L2
python scripts/record_scores.py --covariate                              # L3
python scripts/learning_loop_demo.py                                     # L8
uv run pytest tests/fitness/test_assignment_within_cohort.py             # L1
uv run pytest tests/fitness/test_outcomes_seam.py tests/fitness/test_readout.py
DATABASE_URL=postgresql://agent:agent@localhost:5434/agentstack uv run pytest tests/durability
```

## Project Structure

```
src/agentstack/context/assignment.py   L1  hash-based arm assignment inside the frozen cohort
src/agentstack/context/outcomes.py     L2  the seam: load outcomes with provenance
src/agentstack/prediction/covariate.py L3  eLTV_pre, recorded
src/agentstack/context/readout.py      L4  ANCOVA and INSV, pure
src/agentstack/policy/conclusions.py   L5  Readout → promote / stop / extend
src/agentstack/context/learnings.py    L6-7 write and recall learnings
scripts/simulate_outcomes.py           L2  simulation lives here, never in src/
```

## Code Style

Match the surrounding modules. Pure functions for the statistics (no I/O, no clock). Frozen
dataclasses for `Readout`, `Outcome` and `Learning`. A `simulated: bool` field with no default,
so forgetting it is a type error rather than a silent `False`.

## Testing Strategy

Each task names its proving test (see the todo). Statistical tests run over 100 simulated seeds
with the thresholds stated in the acceptance. Durability tests kill the worker mid-readout.
Nothing in this part may weaken an existing gate. New gates are added, never swapped in.

---

## Layer Ownership Ledger

| # | Layer | Touched | What changes | Test that proves it |
|---|---|---|---|---|
| 1 | Interfaces | no | Slack text gains the conclusion message, rendered by the existing adapter. | existing adapter tests |
| 2 | Control plane | no | The conclusion is keyed to the existing experiment run. | `test_session_ownership` |
| 3 | Runtime | **yes** | A conclude step at the pre-declared endpoint (durable timer), the readout as an activity, exactly one conclusion on replay. | `tests/durability/test_conclude.py`, `replay_guard.py`, `checkpoint_guard.py` |
| 4 | Model engine | **yes, narrow** | Writes the Slack explanation only. Its output reaches no decision. 4b: the covariate scorer (`eLTV_pre`), pinned and recorded. | `test_covariate_is_pre_treatment` |
| 5 | Context / memory | **yes, heavily** | `assignment`, `outcomes`, `readout`, `learnings`; the drafting context gains prior learnings. | `test_assignment_within_cohort`, `test_outcomes_seam`, `test_readout`, `test_learning_memory` |
| 6 | Tools | no | The draft menu stays at five, and the `variant` enum is unchanged. | `test_tool_registry` |
| 7 | Execution surfaces | no | Promote goes through the existing rollout path. | `test_capability_is_not_execution` |
| 8 | Identity / policy / approvals | **yes** | `policy/conclusions.py`. Promote still needs an approval bound to the payload. Stop uses `HALT`. | gates `a-negative-insv-never-promotes`, `a-promotion-still-requires-approval` |
| 9 | Observability | **yes** | Spans for readout and conclusion (allowlisted), audit row for each conclusion. | `test_span_attributes`, `test_audit_separate_from_traces` |
| 10 | Infrastructure | no | One more Postgres use (memory). No new service. | n/a |

## Foundation Assumptions

| Concern | Assumption |
|---|---|
| Delivery semantics | The conclude timer and the readout activity are at-least-once, so a conclusion is keyed on `(experiment_version, endpoint)` and writes once. |
| Consistency | Memory is Postgres, read-committed. `write()` is idempotent on the conclusion key. |
| Isolation / failure | A worker killed mid-readout resumes and produces the same Readout. The readout is deterministic given its inputs. |
| Model asset | `qwen3:8b` for the explanation, `think=False`. The covariate uses the pinned TabPFN checkpoint. |
| Serving system | Ollama locally. The explanation is short and nothing waits on it. |
| Interaction contract | The explanation has no tools. The model sees the Readout as data and cannot change it. |
| Budgets | Readout over a cohort of ~50k rows: seconds (OLS, 2 regressors). Context: up to three learnings, each bounded, inside the existing 8192 window. |

## Boundary Decisions

| Question | Decision |
|---|---|
| Session identity vs authorization | Unchanged. The conclusion belongs to the experiment run. A promote needs a fresh approval. |
| Transcript vs prompt context | Learnings are assembled into the prompt by `context/assemble.py`, never read from a transcript. Prompts stay out of Temporal history. |
| Memory: what, who scopes, when written | Memory now has one writer: the conclude step. Scope is the tenant. Written once per conclusion with provenance (the readout id) and a TTL. The model's hypothesis is a separate `UNTRUSTED` item. No other path writes memory. |
| Capabilities vs execution | Unchanged. Reading a learning is a context operation and executes nothing. |
| Approval boundary | A promote goes through the existing approval path. A stop is `HALT`. The model's explanation changes neither. |
| "This resource, now" | `(experiment_id, experiment_version, data_as_of)` as in `SPEC.md`. `experiment_version` already covers the targeting model and threshold. L3 adds the covariate checkpoint, and L4 adds the outcome window and the estimator. |
| Evidence and judging | Readout rows are committed in `docs/evidence/readout_simulation.csv`. Judged by the L4 coverage and false-positive tests and by the new gates. |

## Boundaries

- **Always:** label every number that comes from planted effects `simulated`. Compute INSV from
  observed columns only. Draw control from the frozen cohort.
- **Ask first:** a dependency (for example `statsmodels`); changing a `.importlinter` contract;
  a minimum INSV above zero; any real outcome source.
- **Never:** draw control from outside the cohort (regression to the mean); let a predicted
  quantity into the INSV; let model text reach a policy decision; write memory from model
  output; let a recalled learning widen the offer menu; hide `simulated` in a message.

## Success Criteria

These become fitness tests or gate cases.

1. **Assignment is deterministic and inside the cohort.** Stable across processes; the split is
   within ±2pp of target at n = 1,000; a subscriber outside the cohort has no arm. (L1)
2. **Outcomes before the rollout are refused**, as are rows for subscribers outside the cohort.
   Provenance, including `simulated`, is carried. (L2)
3. **With τ = 0 the arms' outcome distributions match** in the simulator. (L2)
4. **The covariate is pre-treatment**, pinned and round-trips from the recorded file. (L3)
5. **The readout is calibrated.** Over 100 seeds the 95% interval covers the planted τ in
   ≥ 92, the false-positive rate at τ = 0 is ≤ 8%, and the adjusted interval is on average no
   wider than the unadjusted one. (L4)
6. **INSV reads only observed quantities**, assertable from its input schema (`SPEC.md` 12). (L4)
7. **A negative INSV never promotes**, and **a promotion still needs an approval**. (L5)
8. **A conclusion is written exactly once**, through a killed and resumed worker. (L5, L6)
9. **Memory has one writer.** First-party numbers come only from the Readout. The hypothesis is
   `UNTRUSTED`. (L6)
10. **A concluded experiment reaches the next draft**, another tenant's never does, and an
    injected hypothesis in memory does not widen the menu. (L7)
11. **Two experiments run end to end**, and B's context contains A's readout id. (L8)

## Open Questions

1. **Saved LTV (decision 4)** is proposed above and needs your confirmation before L4.
2. **Effect table and costs for the five offers.** They are invented, as simulated outcomes
   require. At least one offer must lose money so there is something to learn. Checkpoint L-A
   is where you review whether they are credible.
3. **Outcome window and endpoint.** Proposed: the outcome window is the 30 days after the
   cutoff, matching the KKBox label. To be confirmed against the frozen cohort in L2.
