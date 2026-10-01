# Spec: KKBox as a third cohort, via RevenueCat's webhook schema

Status: **rev 3 — approved; amended 2026-10-01 after the data exploration** (see
`docs/evidence/kkbox-exploration.md` and `kkbox-exploration-reading.md`).
Date: 2026-10-01
Module id: `kkbox-cohort` (single capability; Phase 0 scope check: no capability map needed —
the mapping, the cohort spec and the scorer have one consumer, the existing
targeting → experiment → approval path, and none ships or is verified apart from the others)
Companion to `SPEC.md` (Experiment Operator, rev 10). `SPEC.md` is untouched: this file is
named like `SPEC-registry.md` rather than overwriting an approved spec.

---

## Assumptions

Inferences, not decisions you made. Correct any of them before planning.

1. **Placement is inside the agent**, TabPFN only, no Kaggle submission, no leaderboard
   goal (your revisions to the plan, taken as final).
2. **Hosted TabPFN is the scoring path** (`tabpfn-client`, egress to PriorLabs), chosen
   because local TabPFN on ~50k rows × 5 folds is hours on a non-CUDA machine. The
   recorded scores (local, uncommitted) remain the replay path.
3. **Raw inputs (amended):** `train_v2.csv` (label), `members_v3.csv`, v1 `transactions.csv`
   (the history, to 2017-02-28) and `transactions_v2.csv` (rows up to the cutoff only; 75% of
   it is the March label window and is never a feature). `user_logs*.csv` (~30 GB) is **not** used; listening logs are listed as extras in the
   ADR, not forced into the RC schema.
4. **The label stays Kaggle's `is_churn`** (no renewal within 30 days of expiry). The
   RC-event layer is the *feature* representation; it does not redefine churn.
5. **Cutoff 2017-02-28** (amended): fixed by the data, not chosen. The v2 labels are for users
   expiring around March 2017 and the transactions end 2017-03-31. Features use only
   transactions at or before it.
6. **Left-censoring** (measured: only 36% of users' first observed transaction is within 30
   days of registration; 49% registered before 2015-01-01). A user's first transaction *in the
   window* is not necessarily an initial purchase (KKBox data starts 2015-01). Only a first transaction within the
   window of registration is `INITIAL_PURCHASE`; otherwise it is `RENEWAL`, and the count
   of such users is recorded. Registration date is read to decide this, then dropped.
7. **KKBox has no explicit trial flag.** `period_type` is derived (`TRIAL` for a
   zero-amount paid transaction on a priced plan, else `NORMAL`; no `INTRO`), stated as
   an assumption in the ADR, never presented as observed.
8. **Revenue is NTD, not USD** — *decided:* `DatasetSpec` gains a `currency` field
   (existing cohorts default `USD`), and `targeting.py` renders it in the approval text.
   No conversion: a converted figure would be a modelled quantity.
9. **The scored sample is a declared cut** (~50k users, stratified on the label,
   seed-pinned, drawn in the offline build script) and is never widened to meet a gate.
   Hosted limits (rows per fit, quota) are unmeasured; one call sizes the cut, and the
   measured limit is recorded, not the other way round.
10. **Scores are never committed** — *decided.* `data/scores/` is already gitignored
    (`data/*`, only `manifest.json` excepted). Consequence: CI has no recorded KKBox scores,
    so KKBox in CI runs on the fixture and a fake client; the real-score path and the
    end-to-end smoke run are local (`tests/live` fails, not skips, without them).
11. **Scoring is a read**, not an effect: no idempotency-ledger entry (CONSTRAINTS: "no
    read entered in the idempotency ledger"). It is still egress, so it is bounded and
    audited.

---

## Objective

Make KKBox's subscription history a real cohort in the Experiment Operator, so a trigger
can name `kkbox-churn` and the run takes it through the same targeting → experiment →
approval path as `telecom-bigml` and `bank-churn`.

Three moves, each a separate thing:

1. **Re-express** KKBox transactions as RevenueCat webhook events
   (`INITIAL_PURCHASE`, `RENEWAL`, `CANCELLATION`, `EXPIRATION`, with `period_type`,
   `purchased_at_ms`, `expiration_at_ms`, `app_user_id`) so the cohort speaks the schema
   the agent will see in production.
2. **Collapse** those events to one row per subscriber (per-user features at the cutoff),
   written offline to `data/kkbox-cohort.csv` (gitignored); `datasets.load` then treats it
   as any file.
3. **Score** it out of fold with hosted TabPFN through the existing `ChurnScorer` Protocol,
   record the scores, and use the recordings in CI and replay.

**Why it fits.** KKBox has an observed paid amount, so unlike `bank-churn` it is
*targetable* under the observed-ARPU floor. It is the first cohort where a recurring-revenue
event stream, not a flat table, is the source.

**Success looks like**
- `uv run pytest tests/fitness` is green with no data, no token and no network
  (mapping fixture, fake hosted client). KKBox recorded scores are local only.
- `uv run pytest tests/live` is green locally: cohort matches `data/manifest.json`, no
  feature |corr| > 0.9, recorded scores match the real hosted model, out-of-fold log loss
  beats the prior.
- A local worker run with the recorded scorer takes `kkbox-churn` from trigger to a Slack
  approval prompt that names a headcount and a correctly-denominated value at risk.

### Exploration step (added 2026-10-01; done, outcome below)

**Outcome (decided 2026-10-01):** ship `is_churn` only. Spend-after-cutoff is deferred (97.0% of
renewers pay their last amount, so it is last price x not-churned), plan-move deferred (2.5% of
users), time-to-churn and cancel-after rejected (censored / a symptom). No new 4b Protocol.


Before the cohort is built, a read-only exploration of the raw files asks what *else* the
data could predict: next-period revenue (spend after the cutoff), plan upgrade/downgrade,
auto-renew switched off, time-to-churn, alongside `is_churn`. It is **local only** (no
hosted call, no egress), outputs aggregates only (no user-level rows: the data is
third-party), and decides nothing by itself. Rules it must respect:
- a candidate target is computed from **post-cutoff** transactions only, and never overlaps
  the feature window (the same leakage rule as the features);
- `ChurnScorer` is a binary-classification Protocol; a non-binary target (revenue) needs a
  new 4b Protocol, which is a layer-4b change and an ADR, not a flag;
- a **predicted** revenue is a modelled quantity, so it can never be the value-at-risk
  floor (CONSTRAINTS: "no value at risk from a modelled quantity"); observed
  `actual_amount_paid` stays the floor, and a revenue target would be advisory/analytic;
- any target the exploration recommends is a **spec amendment and a re-plan, approved by
  you**, never added silently. Until then the cohort ships churn-only as specced.

### Not in scope
Kaggle submission or leaderboard work; GBDT; `user_logs`; any production RevenueCat
webhook ingestion; a DuckDB path (ask-first dependency, only if the pandas build proves
too slow); widening the sample to satisfy a gate.

---

## Tech Stack

Python 3.12, pandas, uv, pytest, mypy `--strict`, ruff, import-linter, Postgres (untouched).
- **New dependency (ask-first, needs your approval):** `tabpfn-client`, in the existing
  `prediction` extra beside `tabpfn`. mypy override scoped to `tabpfn_client.*`, as for
  `tabpfn.*` (config entry, not an inline suppression).
- The client API (token setting, fit/predict surface, row limits) is **verified against the
  installed version's docs before step 5**, not from memory.
- Data: Kaggle `kkbox-churn-prediction-challenge` (competition download via the `kaggle`
  CLI; rules must be accepted by you on Kaggle first).

## Commands

```
uv sync --extra prediction
uv run python scripts/fetch_datasets.py                       # now also fetches KKBox raw files
uv run python scripts/build_kkbox_cohort.py                   # raw -> data/kkbox-cohort.csv
uv run python scripts/fetch_datasets.py --record              # rewrite data/manifest.json
uv run python scripts/record_scores.py --datasets kkbox-churn # hosted scorer, TABPFN_TOKEN from .env
uv run pytest tests/fitness/test_kkbox_mapping.py tests/fitness/test_data_snapshot.py \
  tests/fitness/test_targeting.py tests/fitness/test_prediction_gate.py \
  tests/fitness/test_layer_boundaries.py
uv run lint-imports
bash scripts/check_fast.sh      # every edit
bash scripts/check_task.sh      # turn end
uv run pytest tests/live        # locally, before the cohort is used in an experiment
uv run python scripts/stack_guard.py --base main
```

## Project Structure

```
src/agentstack/context/kkbox.py          NEW  layer 5: transactions -> RC events; per-user features
src/agentstack/context/datasets.py       EDIT layer 5: REGISTRY["kkbox-churn"]; DatasetSpec.currency
src/agentstack/context/targeting.py      EDIT layer 5: render `currency` in the approval text
src/agentstack/execution/hosted_scorer.py NEW layer 7: HostedTabPFNScorer (egress to PriorLabs)
src/agentstack/prediction/               layer 4b: ChurnScorer / fold_assignment / _encode REUSED;
                                              hosted preflight variant in licence/engine seam
src/agentstack/interfaces/worker_cli.py  EDIT layer 1: wires the hosted scorer; preflight_cli.py too
scripts/build_kkbox_cohort.py            NEW  offline, outside the package (reads Kaggle files)
scripts/fetch_datasets.py                EDIT competition download; manifest entry
scripts/record_scores.py                 EDIT only to select the hosted scorer for this dataset
tests/fitness/test_kkbox_mapping.py      NEW  runs in CI on a tiny committed fixture
tests/fixtures/kkbox/                    NEW  hand-built transactions/members/labels, committed
tests/live/test_real_cohorts.py          EDIT REGISTRY-enumerated: now requires the KKBox files
data/kkbox-cohort.csv                    gitignored
data/scores/kkbox-churn.json             gitignored, local only (decided: no score is committed)
data/manifest.json                       committed
docs/adr/0012-kkbox-cohort-and-hosted-scorer.md   NEW
.importlinter                            EDIT contract 3: add tabpfn_client (a tightening)
pyproject.toml                           EDIT extra + mypy override (ask-first)
```

Module placement of `hosted_scorer.py` is confirmed against `agentstack.execution`'s
actual shape in the plan; the invariant fixed here is *layer 7, never layer 4b*.

## Code Style

Match the surrounding modules: frozen slotted dataclasses, docstrings that say *why*, errors
that name the command that fixes them, no suppression comments.

```python
@dataclass(frozen=True, slots=True)
class RCEvent:
    """One subscription event in RevenueCat's webhook shape. Fields with no RC analogue
    (payment_method_id) are carried in `extras`, never forced into a schema field."""

    type: Literal["INITIAL_PURCHASE", "RENEWAL", "CANCELLATION", "EXPIRATION"]
    app_user_id: str
    period_type: Literal["NORMAL", "TRIAL"]
    purchased_at_ms: int
    expiration_at_ms: int
    extras: Mapping[str, object] = field(default_factory=dict)
```

Every drop has a reason in `drops`; the same data gives the same `data_as_of` (content
hash, never a clock).

## Testing Strategy

| Level | What | Where | Runs |
|---|---|---|---|
| Fitness (no data) | Event type per case (first/in-window/renewal/cancel/expire); cutoff respected; **no post-cutoff leakage** (a transaction after cutoff changes no feature) | `tests/fitness/test_kkbox_mapping.py` on committed fixture | every edit / CI |
| Fitness | Same data → same `data_as_of`; kkbox spec is targetable (has revenue); `bank-churn` still `RevenueNotObserved` | `test_data_snapshot.py`, `test_targeting.py` (extended) | every edit |
| Fitness | Hosted scorer with a fake client recording which rows each call was fit on: no row predicted by a model that saw its label; refuses a served model version it cannot record; hosted preflight; missing token refused at startup | `test_prediction_gate.py` (extended) | every edit |
| Fitness | `execution` may hold the hosted client; `prediction`, `context`, etc. may not import `tabpfn_client` | `test_layer_boundaries.py`, `lint-imports` | every edit |
| Live (local) | Cohort matches manifest; no |corr| > 0.9; recorded scores match the real model; OOF log loss beats the prior | `tests/live` | before use in an experiment |
| End to end | Worker + `RecordedScorers` + `kkbox-churn` → candidate → approval | existing smoke path, **local only** (scores are not committed) | before use |

Coverage: changed lines ≥ 80%. The few lines that construct the real hosted client cannot
execute in CI (no token, no network); like the five already named in `CONSTRAINTS.md`,
they are exercised by `tests/live`, and `CONSTRAINTS.md` states the new count rather than
hiding it. No `pragma`.

---

## Layer Ownership Ledger

| Layer | Touched? | What changes | Fitness test that proves it |
|---|---|---|---|
| 1 Interfaces | **Yes (wiring only)** | `worker_cli.py` / `preflight_cli.py` construct the hosted scorer and run its preflight; no logic | `test_prediction_gate.py` (startup refuses a missing/invalid token), end-to-end smoke |
| 2 Control plane | No | Session ownership unchanged; a trigger naming `kkbox-churn` is an ordinary tenant-checked trigger | `test_session_ownership.py` stays green |
| 3 Runtime / durability | No | No workflow or activity change; scoring happens in the existing turn host. Workflow code still has no path to prediction or execution (contract 6) | `test_temporal_boundaries.py`, `replay_guard.py` stay green |
| 4 Model engine (LLM) | No | Ollama/qwen3:8b unchanged. TabPFN is *not* layer 4 (STACK: separate package on purpose) | `test_ollama_contract.py` stays green |
| **4b Tabular inference** | **Yes** | `ChurnScorer`, `fold_assignment`, `_encode`, `RecordedScorers` reused; hosted preflight variant; `model_version` provenance records what the service served; a recorded `data/scores/kkbox-churn.json` | `test_prediction_gate.py` (fake client: out-of-fold; version provenance; preflight) |
| **5 Context / memory** | **Yes** | `kkbox.py` (RC mapping, per-user features), `REGISTRY["kkbox-churn"]` with target, drops-with-reasons, revenue columns, `currency`. Every column dropped has a recorded reason; `data_as_of` is a content hash | `test_kkbox_mapping.py`, `test_data_snapshot.py`, `test_targeting.py`, `test_context_assembly.py` stays green |
| 6 Tools | No | No tool added or widened; the registry and stage menus are unchanged | `test_tool_registry.py`, `test_registry_tools.py` stay green |
| **7 Execution surfaces** | **Yes** | `HostedTabPFNScorer`: the one place `tabpfn_client` (egress to PriorLabs) is imported, implementing the 4b Protocol. Not a gateway surface: a read, no ledger entry. Egress target is a fixed, checked constant (ADR-0004: a real client arrives with its containment dimension) | `test_layer_boundaries.py`, `lint-imports` (contract 3 tightened), `test_containment.py` |
| 8 Identity / policy / approvals | No | No approval tier or rule changes; a scorer returns a probability and decides nothing. Approval text names the cohort's currency (`currency` field) | `test_approval_tiers.py`, `test_approval_legibility.py` stay green |
| **9 Observability / eval** | **Yes (minimal)** | The hosted call is traced/audited as egress using an *existing* span type; no span type removed; none added unless the plan finds no fit (then the count bump is stated, not slipped in) | `test_trace_completeness.py`, `test_audit_separate_from_traces.py`, `evals run --gates` |
| 10 Infrastructure | No | No migration; Postgres untouched; files only (`data/`) | `tests/infra` stays green |

## Foundation Assumptions

- **Delivery semantics.** Scoring is **best-effort request/response, at-least-once by
  retry**, and that is safe *only because it is a pure read*: re-scoring the same rows
  cannot double an effect. The hosted service may be non-deterministic across calls; that
  is why the **recorded scores, not live calls, are the replay path (local, never committed)**, and why an
  experiment's cohort is frozen from scores in `experiment_version`. Nothing here enters
  the idempotency ledger. Gateway-routed effects (rollout) keep exactly-once-by-key.
- **Consistency model.** The cohort is an offline file addressed by content hash; there is
  no live store. Scores are valid only for one `(dataset, data_as_of)`; `RecordedScorer`
  already refuses any other snapshot or a different positive count.
- **Isolation & failure semantics.** Hosted unreachable/timeout/quota-exhausted →
  `ScoringError`/`LicenceRefused`, **refused, never retried silently and never degraded to
  a guess**. A missing/invalid token is refused at startup (criterion 18), not mid-run. A
  scorer that cannot record the served model version refuses, rather than recording a
  version it did not use. Process restart is unaffected: recorded scores are files.
- **Model asset.** TabPFN as served by PriorLabs; **the checkpoint is chosen server-side**,
  so the asset is not pinned by us. Mitigation: record what was served in `model_version`
  provenance. Local weights are non-commercial licence; hosted terms must be read before
  any commercial use (open risk, below). The LLM asset is unchanged.
- **Serving system.** PriorLabs hosted API, behind a token. Queueing, batching, row limits,
  rate/credit quota and tail latency are **unmeasured**. One real call measures them before
  the cut is sized; the measurement goes in ADR-0012.
- **Interaction contract.** `tabpfn_client` fit/predict-style API behind our `ChurnScorer`
  Protocol (`score(features, labels, dataset, data_as_of) -> ChurnScores`). The Protocol
  has no threshold; fold assignment and encoding stay ours so out-of-fold is checkable.
- **Budgets.**
  - *Context tokens:* unchanged; the cohort never enters a prompt, only a headcount, a
    value at risk and the predicate do.
  - *Latency:* offline scoring, not on the turn path (turns read recorded scores). The
    live hosted call's per-fit and per-fold latency is recorded, not budgeted, until measured.
  - *Cost:* PriorLabs credits for 5 fits + predicts over the cut — **measured with one
    call, then stated** before the full run; a quota that cannot cover the cut shrinks the
    cut (declared in the ADR), never the folds.
  - *Egress:* data leaves the machine. Rows go to a third party; all features are sent (decided); see Decisions taken #2.

> Compatible is not equivalent. The hosted API is not evidence the local `tabpfn` gives the
> same numbers. They are two serving systems with two `model_version`s and are never
> blended under one provenance string.

## Boundary Decisions

| Question | Decision |
|---|---|
| **Session vs authorization.** What owns session identity, and how is it distinct from authorization? | Unchanged. The run/session record owns identity; a trigger for `kkbox-churn` is tested against the run's own tenant in layer 8 before scoring. Scoring needs a licence token, which is *capability to call a vendor*, not authority to act on anyone: possessing `TABPFN_TOKEN` grants no permission over a rollout. |
| **Transcript vs context.** What is the transcript store, and how is prompt context derived from it? | Unchanged. The transcript stays the durable record. Raw KKBox rows and RC events are **never** in a prompt or in workflow history; context gets a derived, labelled view (headcount, value at risk with currency, predicate, provenance), each item with scope, provenance, freshness, reason, trust. |
| **Memory vs learning.** What is memory, who scopes it, when is it written? | The cohort and recorded scores are **data**, not memory, and TabPFN does not learn: it conditions on rows per call (no weight update, nothing persists server-side that we rely on). No memory is written because a model said something; `memory.write()` stays the only door. |
| **Capability vs execution.** Which capabilities are exposed, and which surface executes them? | **No new tool is exposed.** Scoring is invoked by the worker, not offered to the model. The client that reaches PriorLabs lives only in layer 7; `context` (kkbox.py) and `prediction` import no HTTP client (contract 3 + `tabpfn_client` added). A tool function never gets its own I/O. |
| **Approval vs isolation.** Where is the approval boundary, and what isolates the action afterwards? | Unchanged and *later than the scoring*: approval stays immediately before the rollout, bound to run id, action fingerprint and a state snapshot. Scoring is a read and needs none. Isolation for the new egress is a fixed host bound (ADR-0004) plus scoring being unable to commit anything. Neither stands in for the other. |
| **What identifies "this resource, now"?** | `experiment_version` freezes `(kkbox-churn, data_as_of, model_version, folds, seed, scored_rows, positives)`; the approval's snapshot reads the rollout state at the act. A moving hosted service cannot move a frozen cohort because the recorded scores, not a fresh call, define it. |
| **Observability vs evaluation.** What evidence is emitted, and what criteria judge it? | Evidence: an egress trace/audit record for each hosted call (rows, folds, served version, latency, outcome — never the rows' contents or the token). Criteria live elsewhere: fitness tests (out-of-fold, boundaries), `tests/live` (log loss beats prior, scores match the real model), and `evals/` gates. A clean trace of a leaking scorer would still fail `test_prediction_gate.py`. |

## Boundaries

- **Always:**
  - route every real side effect through `agentstack.execution.gateway.execute` with an
    identity envelope and an idempotency key (this change adds none; the rollout is unchanged)
  - attach scope, provenance, freshness, reason and trust to every context item derived
    from the cohort
  - record a reason for every dropped column; derive `data_as_of` from content
  - score out of fold: no row predicted by a model that saw its label
  - treat missing/invalid `TABPFN_TOKEN` as a startup refusal
  - run `check_fast.sh` per edit and `check_task.sh` at turn end
  - document the decisions and measured limits in ADR-0012
- **Ask first:**
  - adding `tabpfn-client` (a dependency) — **approved 2026-10-01**
  - editing `.importlinter` contract 3 (a tightening; confirm the exact module in the plan)
  - adding `duckdb` (only if the pandas build proves too slow)
  - the first hosted call: only after the feature set is agreed (Open Question 1)
  - adding a span type (it raises the tracked count) or any tool / scope
- **Never:**
  - expose a tool that performs its own side effect, or give any tool function its own I/O
  - write memory implicitly because the model said something
  - approve at task start for an act that happens later
  - let untrusted content (tool output, retrieved documents, user-supplied text, the raw
    KKBox/members fields) influence an authority decision
  - import the database driver outside `agentstack.storage`, or edit an applied migration
  - widen the sample, loosen the |corr| bar, or edit `CONSTRAINTS.md` / `.importlinter` to
    make a gate pass
  - record a hosted score under a model version the service did not report
  - commit raw Kaggle data, the Kaggle token, or `TABPFN_TOKEN`

## Success Criteria

1. `tests/fitness/test_kkbox_mapping.py` proves, with no data: each fixture case maps to the
   right RC event type; a transaction after the cutoff changes no feature; left-censored
   users are not `INITIAL_PURCHASE`.
2. `REGISTRY["kkbox-churn"]` loads, and the same data gives the same `data_as_of` twice.
3. `annual_revenue_cents` works for `kkbox-churn`; `bank-churn` still raises
   `RevenueNotObserved`. The approval text names the cohort's currency (`NTD` for KKBox, `USD` unchanged elsewhere).
4. The fake-client test shows no row is predicted by a model that saw its label; a served
   version it cannot record is refused.
5. `lint-imports` and `test_layer_boundaries.py` are green with `tabpfn_client` confined to
   `agentstack.execution`.
6. `data/manifest.json` has the `kkbox-churn` entry; `data/scores/kkbox-churn.json` is
   recorded locally (uncommitted); `tests/live` passes locally (no |corr| > 0.9; OOF log loss beats the prior).
7. A worker run with `RecordedScorers` takes `kkbox-churn` to an approval prompt.
8. `stack_guard.py --base main` reports no weakened bar; fitness test count and coverage
   do not fall; any counts that rise are bumped in `CONSTRAINTS.md` and the README in the
   same change.

## Decisions deferred to ADR-0012 (`docs/adr/0012-kkbox-cohort-and-hosted-scorer.md`)

Written at plan time, in the style of ADR-0002 (options, what would decide it, invariants
fixed meanwhile):

1. Flat offline CSV from a multi-table source (build script, outside the package).
2. Sampling as a declared cut; measured hosted limits; DuckDB only on proof.
3. Exact cutoff date; label alignment; the left-censoring rule.
4. Drops with reasons: `msno`, raw registration/expiry dates, anything |corr| > 0.9.
5. Revenue: last pre-cutoff `actual_amount_paid`, annualised by a stated
   periods-per-year (most plans are 30 days), in `revenue_note`; the `currency` field.
6. RC mapping fidelity and `extras`.
7. Hosted placement (layer 7), egress bound, contract 3 tightening, read-not-effect.
8. Model-version provenance for a server-chosen checkpoint; recorded scores as replay path.
9. Data egress and competition rules answer; hosted licence terms.

## Feature set (agreed 2026-10-01; population and cutoff amended after exploration)

**Population (amended).** **`train_v2` as given**: the 968,436 labelled users with any
transaction up to the cutoff, churn 8.9%. Re-deriving "February-active March expirers" was
tested and rejected: it drops users with no February transaction, who churn at 65.8%.
**Cutoff 2017-02-28**; every feature uses only transactions at or before it. The label is
Kaggle's `is_churn`, used as given (our recompute from transactions agrees for 95.1%).

**Rule for raw columns.** A raw column is admitted if it can improve prediction **and**
passes the leakage rules: it is known before the cutoff, it is not an identifier, and its
|corr| with the target is ≤ 0.9. It is excluded otherwise, with a reason in `drops`.

| Group | Features | Source |
|---|---|---|
| Tenure/history | tenure days at cutoff, raw registration date (as a number), observed transaction count, distinct plan changes, left-censored flag | `members_v3`, transactions |
| RC events | count of each of the four event types, days since last event | the RC mapping |
| Current plan | last `payment_plan_days`, `plan_list_price`, `actual_amount_paid`, `is_auto_renew`, `payment_method_id` | last pre-cutoff transaction |
| Value | paid vs list ratio, trial flag, total paid to date | transactions |
| Cancellation | cancel count, last-transaction-is-cancel | `is_cancel` |
| Demographics | `city`, `registered_via`, `gender` (missing = its own level), `bd` (values outside 10–90 → null, plus a missing flag) | `members_v3` |
| Expiry | days from cutoff to last `membership_expire_date`, **and the raw expiry date as a candidate** | transactions |

- **Out, with reasons:** `msno` (identifier: names the row; a hash carries no signal and
  adds memorisation risk); anything |corr| > 0.9; `user_logs*` (decided out of scope: ~30 GB,
  revisit after the first TabPFN result).
- **Candidates, decided empirically once.** The raw registration date and raw expiry date can
  encode the label window, so they are *candidates*: the build compares out-of-fold log loss
  with and without them in one declared comparison (small sample, run once, recorded in the
  ADR). A candidate that wins and passes the rules stays; the comparison is not repeated
  until one gets a better number, since that is tuning against the gate.
- **All features are sent to the hosted scorer.** No privacy filter on the engineered columns.

## Decisions taken (2026-10-01)

| # | Decision |
|---|---|
| 1 | Add `currency` to `DatasetSpec`; render it in the approval text. No FX conversion. |
| 2 | Send everything to the hosted scorer. Raw columns are in if they improve prediction and pass the leakage rules (see Feature set). |
| 3 | `tabpfn-client` approved. Still yours: accept the Kaggle rules; the hosted preflight proves the token works. |
| 4 | No score is committed. |
| 5 | `user_logs` stay out. |
| 6 | `bd` outside 10–90 → null with a missing flag. |
| 7 | Cohort is users with an expiry in the label window. |

## Open Questions

1. **Plan truncation:** decisions 2–3 of your pasted plan were cut off; worth a glance.
2. **Competition rules on third-party processing:** not verified by me; the ADR will say
   "decided by the owner, unverified" unless you tell me you've checked.
3. **Raw-column comparison costs hosted credits.** It runs on a small sample; the size is
   set after the one measuring call.
