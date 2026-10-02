# Implementation Plan: KKBox as a third cohort

Derived from `SPEC-kkbox-cohort.md` (approved 2026-10-01). Task list: `tasks/todo-kkbox-cohort.md`.

**Amended 2026-10-02: KKBox is scored locally, not by the hosted service.** Owner decision,
recorded in ADR-0012 §6: Kaggle competition data is not sent to a third party. K12 and K14 below
are rewritten to local `TabPFNScorer`; Phase B (K7-K10, the hosted scorer) stays built and is
kept for cohorts cleared to leave the machine, but nothing in this plan calls it on KKBox. The
spec still names hosted as the scoring path (Decisions taken #2, Foundation Assumptions); this
plan overrides it until the spec is amended (see Open questions 3).
Separate from `tasks/plan.md` / `tasks/todo.md` (Experiment Operator iteration 1, still has
open items): decided 2026-10-01 so neither plan overwrites the other. Task ids are `K1…`.

## Overview

Make `kkbox-churn` a cohort the existing trigger → target → experiment → approval path can
run: KKBox transactions re-expressed as RevenueCat webhook events, collapsed to one row per
subscriber, scored out of fold by local TabPFN, with the currency (NTD) shown honestly in
the approval prompt. Nothing here weakens `CONSTRAINTS.md` or `.importlinter`; one contract
is tightened (contract 3 gains `tabpfn_client`) and two CLI edges are ignored (K9, ADR-0012).

## What the code told us (changes the spec's shape, not its intent)

1. **`$` is hard-coded in three layers**, not one: `context/targeting.py:205`,
   `runtime/operator.py:110`, `interfaces/slack.py:94` (the approval prompt itself). The
   spec's "add `currency`" is therefore three single-layer tasks (K1 layer 5, K2 layer 3,
   K3 layer 1), each defaulting to USD so nothing moves until KKBox is registered.
2. **`frozen_cohorts.py` carries the value into `experiment_version`.** Adding a currency
   field must not change the fingerprint of any existing USD cohort, or live runs and
   recorded histories strand. K1 proves this (`replay_guard`, `test_checkpoint_guard`).
3. **`engine._encode` was private to layer 4b**, and the hosted scorer (layer 7) had to reuse
   it so out-of-fold stays checkable. K8 promoted it to `engine.encode` (a rename, no
   behaviour change). The local scorer uses it as before.
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
   git-ignored so this stays true. `scripts/record_scores.py`'s docstring still says its output
   "IS committed"; true for the two older cohorts, false for KKBox. K14 corrects it.
7. **Local scoring changes the cost, not the shape (2026-10-02).** `TabPFNScorer` already
   exists and `record_scores.py` already uses it (it has no hosted switch). What moves: the
   binding limit is local compute, not a vendor quota. The spec estimated hours for ~50k x 5
   folds without CUDA; that is unmeasured on this machine. K12 now times one local fold on
   a small sample and sizes the cut from it. `ignore_pretraining_limits=True` is already set,
   so row count is a time limit and not a refusal. The licence gate (`TABPFN_TOKEN`, HF-gated
   weights, non-commercial) still applies, because it is the same model asset.

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
- **B — The hosted scorer: the new side effect (egress).** K7–K10, checkpoint B. Built and kept
  for cohorts cleared to leave the machine; **KKBox never calls it** (owner decision 2026-10-02).
  Enforcement in code is ADR-0013's follow-up, not this plan.
- **C — Real data and real scores (needs you).** K11a (fetch + explore) and checkpoint E
  (what the data can predict), then K11–K14, checkpoints C (now: local compute and licence,
  no egress) and D.
- **D — End to end and close-out.** K15–K16.

Checkpoints sit where the plan introduces a **new capability or side effect**: A closes the
last purely-local phase; B is the new egress path built but unused; C is the first time
cohort rows leave the machine; D is before the cohort is used in an experiment. No new
approval boundary is introduced (scoring is a read; the rollout approval is unchanged), and
K15 proves it still sits immediately before the irreversible act. For KKBox, C no longer marks
a first egress: it marks the first long local run and the licence statement.

## Architecture decisions carried in

- `HostedTabPFNScorer` stays in layer 7 (built, K8) for cleared cohorts; KKBox uses `TabPFNScorer`
  (layer 4b, local) and the worker's default `--scores tabpfn` or `recorded`.
- Scoring is a read: no idempotency-ledger entry. Local scoring has no egress; the traced call
  (K10) still records rows, folds, version and latency.
- Fold assignment and encoding stay ours on both paths.
- A local score is recorded under `MODEL_VERSION` (the pinned checkpoint), so the version is
  known by construction; a hosted score is recorded under what the service reports, or refused.
- Counts and the "lines CI cannot cover" paragraph are updated to the truth, never loosened
  (K16); an edit to `CONSTRAINTS.md` is flagged to you, because `stack_guard` will see it.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Currency field alters existing `experiment_version` fingerprints | High: strands live runs | K1 asserts USD fingerprints are byte-identical; `replay_guard` + `checkpoint_guard` in its verify |
| `tabpfn-client` API differs from what the spec assumes | Med | K7 reads the installed version's docs first; K8's fake mirrors the *verified* surface |
| Local run too slow for ~50k × 5 folds on a non-CUDA machine (spec estimated hours) | High: cut must shrink, or the run is not repeatable | K12 times one fold on a small sample *before* the cut is fixed; the cut shrinks, never the folds. The recorded file is what makes it reproducible, so a slow run happens once |
| Memory: ~40k training rows through the local model | Med: OOM mid-run, hours lost | K12 reports peak memory with the timing; the cut is sized to both |
| Weights unavailable or the licence gate fails | Med: blocked | `preflight` already proves load before scoring (`check_token` first); K12 starts with it |
| Non-commercial licence on local weights | Low here, High if deployed | Unchanged from the two older cohorts; stated in README Licensing. Not widened by this change |
| Someone runs `--scores hosted` on KKBox anyway | High: data leaves the machine | Procedural only today (ADR-0012 §6). ADR-0013 is the enforcing follow-up. K14 neither offers the flag nor documents it for KKBox |
| `osv-scanner` flags a transitive dep of `tabpfn-client` | Med: CI red | Dependency stays in the lock (K7: no issues). Removing it is a separate ask-first change, not made here |
| Raw-date features win only by leaking the label window | High: a score that means nothing | K13's rule: known pre-cutoff, not an identifier, |corr| ≤ 0.9, compared once; K14 live test enforces the 0.9 bar |
| Exploration widens scope silently (revenue, upgrade, time-to-churn) | High: a non-binary target is a new 4b Protocol, and predicted revenue breaks the observed-ARPU floor | K11a only recommends; Checkpoint E makes any new target a spec amendment you approve, then a re-plan before K11 |
| Candidate target leaks through the feature window | High | K11a's fixture test: targets from post-cutoff only, zero row overlap with features |
| Exploration output exposes third-party rows | Med: licence | Aggregates only, no `msno`; the committed report has no user-level data; raw data stays gitignored |
| Competition rules may forbid sending rows to a third party | Resolved | Owner decision 2026-10-02: no third-party processing of KKBox. CP-C no longer needs "checked / accepted unverified"; ADR-0012 §6 records it |
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
3. **Spec amended (decided 2026-10-02).** `SPEC-kkbox-cohort.md` carries a dated amendment and
   its headline decisions are updated. Its Layer Ownership and Foundation Assumptions tables
   still describe the built hosted scorer (K7-K10) in hosted terms; they describe that module,
   not KKBox's path, and are left as the record of what was built.
4. **`tabpfn-client` and `HostedTabPFNScorer` stay (decided 2026-10-02)**, unused on KKBox
   (cleared cohorts such as the synthetic SaaS one may use it, ADR-0013).
5. **`TABPFN_TOKEN` comes from `.env` (decided 2026-10-02, resolved).** `record_scores.py`,
   `agentstack-preflight` and the worker (which preflights first) all read `.env` through
   `prediction.licence.load_env`; a variable already exported wins.
