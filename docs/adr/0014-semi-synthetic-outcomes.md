# ADR-0014: Semi-synthetic outcomes for the learning loop

- Status: **accepted** (owner, 2026-10-05, plan decision 4). The seam and the labelling rule
  are decided. The effect table is **not**: it is reviewed at Checkpoint L-A.
- Date: 2026-10-06
- Layers: 5 (the outcomes seam, the readout), 9 (the `simulated` flag in evidence); `scripts/`
  for the simulator

## Context

The learning loop needs outcomes after a rollout. No rollout has ever been measured: a
rollout moves an experiment to `live` in a registry and no customer receives an offer. There
is no outcome source to read.

Waiting for one blocks everything after it. Faking outcomes without saying so would be worse:
a readout built on invented numbers that looks like a result is exactly the "wrong answer that
looks right" `SPEC.md` warns about.

## Decision

Outcomes are **semi-synthetic**, and the line between real and planted is part of the data.

| | Source | Status |
|---|---|---|
| Control arm | Real KKBox post-cutoff renewals and revenue for the frozen cohort | **real** |
| Treatment arm | The same real baseline, plus a planted effect | **planted** |
| The effect | A table keyed on the offer (`VARIANTS`) that sets who responds and by how much, chosen so offers differ and at least one loses money | **planted** |
| Offer cost | Defined per offer in the simulator, recorded on the draft | **planted, observed after** |
| True τ and true INSV | Written beside the outcomes | **planted, known** |

Rules that make this safe:

1. **`simulated: bool` has no default** and travels in provenance from the outcomes seam into
   the Readout, the memory record, the recalled learning and the Slack message. A record
   without it is a type error.
2. **The simulator lives in `scripts/`**, not `src/`. The agent reads outcomes through
   `context/outcomes.py` and cannot import the simulator.
3. **The seam refuses** rows dated before the rollout and rows for subscribers outside the
   cohort, so a leak is caught where the data enters.
4. **The true τ is stored with the data**, which is what lets the readout be tested for
   coverage and false positives. Without a known truth the estimator can only be checked for
   running.
5. **Control comes from the same frozen cohort** as treatment, never the general population
   (regression to the mean, `SPEC.md`).

## What this proves, and what it does not

**Proves:** the ANCOVA recovers a planted effect with calibrated intervals; the decision
policy maps intervals to actions as written; the conclude step is durable and writes once;
memory holds the right things apart; a recalled learning reaches the next draft.

**Does not prove:**
- that any offer retains real customers. The effects are invented;
- that the effect sizes or costs are realistic. Checkpoint L-A is where a person judges them;
- that a model chooses better after reading a learning. That is E8, and it inherits this
  limit: the reported result is about this simulator.

Every README number from Part L carries the word `simulated`.

## Consequences

- When a real outcome source exists, only the file the seam reads changes. Rows then carry
  `simulated: false`, and the simulator is a test fixture.
- **Part D is deferred**, so the effect table is keyed on the five menu offers rather than on
  offer kind and amount (the original L2 dependency on D1). If D returns, the table is re-keyed.
- An agent that learns from planted effects can only learn the planted structure. That is
  acceptable for plumbing and unacceptable as a claim, which is why the rule above is
  mechanical rather than a note.
