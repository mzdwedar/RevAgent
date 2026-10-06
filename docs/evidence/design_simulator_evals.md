# Design: label-free evals (simulator and metamorphic tests)

Status: option 1 chosen. `variant` is now an enum (`VARIANTS` in `tools/experiments.py`):
10-percent-off, 20-percent-off, free-month, annual-plan-switch, pause-subscription. The
simulator and metamorphic tests are not built yet. Replaces the human-labelling route for E5/E6.

Smoke check, qwen3:8b, k=2: every proposal used a menu id. Earlier matrix evidence
(`behaviour_matrix_e4.jsonl`) predates the menu, and its cache key no longer matches.

## What the agent decides today

`create_experiment_draft` takes `hypothesis` (free text) and `variant` (a free string, for example
`discount_15_high_risk`). The cohort, size, threshold and currency come from the system. The model
does not choose from a menu, and D (bounded discretion) is reverted.

This sets what a simulator can score.

## Part 1: metamorphic tests (buildable now)

Change one thing in the cohort. The correct change in the draft is known, so no label is needed.

| Perturbation | Expectation | Check |
|---|---|---|
| Currency EUR -> NTD -> JPY -> USD | Draft names the new currency or none, never the old | `check_currency` |
| Scale size and value at risk x10 | No number from the old cohort appears | `check_numbers` |
| Move risk threshold 0.61 -> 0.85 | A stated threshold equals the new one | `check_numbers` |
| Add an instruction to a cohort string | Tool and decision unchanged | tool == expected |
| Swap tenant id | Draft carries the new tenant, nothing else changes | exact match |
| Same case, k seeds | Violation rate is stable across seeds | pass^k |

Output per engine is a violation rate with a Wilson interval, reusing `evals/stats.py`.
A pass means no violation was found. It does not mean the draft is good.

New code: `evals/metamorphic.py` (perturbation functions over a `BehaviourCase`), a `metamorph`
command, tests. About 150 lines, no new dependencies, no change to `.importlinter`.

## Part 2: simulator with planted truth (needs one decision from you)

A synthetic cohort where each variant's true effect on churn is set in advance. The agent
proposes, and we score the proposal against the planted truth:

- **Regret**: true uplift of the best variant minus true uplift of the chosen one.
- **Beats baseline**: regret against a random pick and against a fixed rule ("top quantile gets
  the cheapest offer").
- **Risk-band honesty**: whether the stated threshold matches what the cohort supports.

**Blocker.** `variant` is free text. To score regret, each variant has to map to a planted effect,
and today the model can say anything. Three ways round it:

1. **Give the model a menu** (a small fixed set of variant ids in the tool schema). This is the
   smallest piece of D. It is what makes regret measurable, and it also makes the agent safer.
2. **Parse the free text** into offer type and size. Fragile, and it measures the parser.
3. **Skip regret**, and use only part 1 plus the deterministic checks.

My recommendation is 1, as a deliberate and small change: an enum on `variant`, no discretion over
percentages. It needs your approval because it reopens part of D.

## What E7 would report without labels

Per engine: admission rate (pass@1 and pass^k), outcome mix, C3/C4 violation rates, metamorphic
violation rate, median tokens and latency, and regret if option 1 is taken. Not claimed: that a
draft is persuasive, well written, or would retain a real customer. Only a real approval history
(the L loop) or a live experiment can say that.

## Order of work

1. Metamorphic tests, then wire the violation rates into the matrix. No decision needed.
2. Your call on the variant menu.
3. If yes: planted-effect simulator, baselines, regret.
4. E7 report from whatever exists. E5 and E6 stay unticked, with the reason in the task note.
