# Implementation Plan: KKBox as a third cohort

Derived from `SPEC-kkbox-cohort.md` (approved 2026-10-01). Task list: `tasks/todo-kkbox-cohort.md`.
Separate from `tasks/plan.md` / `tasks/todo.md` (Experiment Operator iteration 1, still has
open items): decided 2026-10-01 so neither plan overwrites the other. Task ids are `K1…`.

## Overview

Make `kkbox-churn` a cohort the existing trigger → target → experiment → approval path can
run: KKBox transactions re-expressed as RevenueCat webhook events, collapsed to one row per
subscriber, scored out of fold by hosted TabPFN, with the currency (NTD) shown honestly in
the approval prompt. Nothing here weakens `CONSTRAINTS.md` or `.importlinter`; one contract
is tightened (contract 3 gains `tabpfn_client`).

## What the code told us (changes the spec's shape, not its intent)

1. **`$` is hard-coded in three layers**, not one: `context/targeting.py:205`,
   `runtime/operator.py:110`, `interfaces/slack.py:94` (the approval prompt itself). The
   spec's "add `currency`" is therefore three single-layer tasks (K1 layer 5, K2 layer 3,
   K3 layer 1), each defaulting to USD so nothing moves until KKBox is registered.
2. **`frozen_cohorts.py` carries the value into `experiment_version`.** Adding a currency
   field must not change the fingerprint of any existing USD cohort, or live runs and
   recorded histories strand. K1 proves this (`replay_guard`, `test_checkpoint_guard`).
3. **`engine._encode` is private to layer 4b**, and the hosted scorer (layer 7) must reuse
   it so out-of-fold stays checkable. K8 promotes it to a public helper (a rename, no
   behaviour change) rather than copying it.
4. **`fetch_datasets.py` loops `REGISTRY` and calls `datasets.load` for every spec.** The
   moment `kkbox-churn` is registered (K6) that loop would fail on a machine without the
   built cohort file. K6 adds `derived_by` to `DatasetSpec` so the loader and fetch name
   the script that builds it, instead of failing opaquely or skipping.
5. **`tests/live` enumerates `REGISTRY`**, so from K6 until K11 it fails locally for
   KKBox (by design: fails, never skips). CI is unaffected (`tests/live` is excluded).
6. **Scores are not committed** (your decision; `data/*` is already ignored). So there is
   no CI-resident KKBox recording: CI proves the mapping, the spec, the scorer's
   out-of-fold property (fake client) and the approval text on a fixture; the real-score
   and end-to-end paths are local (`tests/live`). K14 adds a test that the score file is
   git-ignored so this stays true.

## Order (changed 2026-10-01): data first

K11a (fetch + explore) → Checkpoint E → K4 (RC mapping) → rest of Phase A. The mapping is
built against what the real data shows. K1–K3 (currency) are independent of this.

## Dependency graph (superseded where it conflicts with the order above)

```
K1 ─► K2 ─► K3                      (currency, layer 5 → 3 → 1)
K4 ─► K5 ─► K6 ──────────────┐      (RC events → features → registry; layer 5)
                             ├─► CP-A
K1 ──────────────────────────┘
K7 ─► K8 ─► K9 ─► K10 ─► CP-B       (hosted scorer; new egress)   [K8 needs nothing from A]
K6 ─► K11a ─► CP-E ─► (re-plan if new targets) ─► K11 ─► CP-C ─► K12 ─► K13 ─► K14 ─► CP-D ─► K15 ─► K16
(K11a needs only K6 and the Kaggle rules, so it can run during Phase B; K11 needs CP-A, CP-B and CP-E)
```
Phases A and B are independent and can run in either order or in parallel sessions; both
must be green before anything touches real data.

## Phases

- **A — Pure, provable in CI with no data, token or network.** K1–K6, checkpoint A.
- **B — The hosted scorer: the new side effect (egress).** K7–K10, checkpoint B *before any
  real call*.
- **C — Real data and real scores (needs you).** K11a (fetch + explore) and checkpoint E
  (what the data can predict), then K11–K14, checkpoints C (before the first real egress of
  rows) and D.
- **D — End to end and close-out.** K15–K16.

Checkpoints sit where the plan introduces a **new capability or side effect**: A closes the
last purely-local phase; B is the new egress path built but unused; C is the first time
cohort rows leave the machine; D is before the cohort is used in an experiment. No new
approval boundary is introduced (scoring is a read; the rollout approval is unchanged), and
K15 proves it still sits immediately before the irreversible act.

## Architecture decisions carried in

- Placement per spec: `HostedTabPFNScorer` in layer 7, wired in by the worker; never in 4b.
- Scoring is a read: no idempotency-ledger entry, but traced/audited egress (K10).
- Fold assignment and encoding stay ours; the vendor only fits and predicts.
- A hosted score is recorded under the model version the service reports, or refused (K8).
- Counts and the "lines CI cannot cover" paragraph are updated to the truth, never loosened
  (K16); an edit to `CONSTRAINTS.md` is flagged to you, because `stack_guard` will see it.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Currency field alters existing `experiment_version` fingerprints | High: strands live runs | K1 asserts USD fingerprints are byte-identical; `replay_guard` + `checkpoint_guard` in its verify |
| `tabpfn-client` API differs from what the spec assumes | Med | K7 reads the installed version's docs first; K8's fake mirrors the *verified* surface |
| Hosted limits (rows/fit, quota) too small for ~50k × 5 folds | High: cut must shrink | K12 measures with one call *before* the cut is fixed; the cut shrinks, never the folds |
| Server-side checkpoint moves between calls | Med: unreproducible cohort | K8 records served version; recorded scores define the cohort; `RecordedScorer` refuses a moved snapshot |
| Egress without a containment dimension (ADR-0004 says a first real client brings one) | Med: `/stack-audit` finding | K8 pins the PriorLabs host as a checked constant with a test; if the auditor reads ADR-0004 as requiring a `Sandbox` field, K8 is revisited before CP-B |
| `osv-scanner` flags a transitive dep of `tabpfn-client` | Med: CI red | K7 runs it locally if installed; otherwise stated as unchecked and checked in CI |
| Raw-date features win only by leaking the label window | High: a score that means nothing | K13's rule: known pre-cutoff, not an identifier, |corr| ≤ 0.9, compared once; K14 live test enforces the 0.9 bar |
| Exploration widens scope silently (revenue, upgrade, time-to-churn) | High: a non-binary target is a new 4b Protocol, and predicted revenue breaks the observed-ARPU floor | K11a only recommends; Checkpoint E makes any new target a spec amendment you approve, then a re-plan before K11 |
| Candidate target leaks through the feature window | High | K11a's fixture test: targets from post-cutoff only, zero row overlap with features |
| Exploration output exposes third-party rows | Med: licence | Aggregates only, no `msno`; the committed report has no user-level data; raw data stays gitignored |
| Competition rules may forbid sending rows to a third party | High | Not verifiable by me. CP-C requires you to say "checked" or "accepted unverified"; the ADR records exactly that |
| Editing `CONSTRAINTS.md` counts trips `stack_guard` | Low | Only state truth (new count of uncoverable lines, new test count); flagged to you in K16 |

## Out of scope (spec)

Kaggle submission/leaderboard, GBDT, `user_logs`, production webhook ingestion, DuckDB (only
if the pandas build proves too slow, as its own ask-first decision), widening the cut to
meet a gate.

## Open questions

1. **K1 fingerprint:** if `currency` must enter the frozen fingerprint for non-USD cohorts
   only, is a conditional field acceptable? (It is the only way USD stays byte-identical.)
   Default: yes.
2. **Where the e2e CI variant gets scores** (K15): a tiny fixture cohort with a fake scorer
   that returns deterministic probabilities, since no KKBox recording is committed. Default: yes.
