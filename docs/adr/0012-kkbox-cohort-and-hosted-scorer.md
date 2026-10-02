# ADR-0012: KKBox as a third cohort, via RevenueCat's webhook schema, scored by hosted TabPFN

- Status: **proposed**. Sections 1-3 are decided and built (K11a, K4). Sections 4-7 are
  decided in `SPEC-kkbox-cohort.md` and become "accepted" as their tasks land; each says what
  would change it.
- Date: 2026-10-01
- Layers: 5 (context), 4b (prediction), 7 (execution), 1 (wiring), 9 (evidence)

## Context

`telecom-bigml` and `bank-churn` are flat tables. KKBox is a subscription event stream with
an observed paid amount, so it is the first cohort that is both *targetable* under the
observed-ARPU floor and shaped like what RevenueCat sends in production. It is third-party
and licensed (gitignored); only aggregates and `data/manifest.json` are committed.

## Decisions

### 1. Inputs, cutoff, population (decided; evidence: `docs/evidence/kkbox-exploration*.md`)

- **History is v1 `transactions.csv`** (to 2017-02-28), plus `transactions_v2.csv` rows up to
  the cutoff. 75% of v2 is the March label window and is never a feature. Not used:
  `user_logs*` (~30 GB), kept out of scope until a first TabPFN result says it is worth it.
- **Cutoff 2017-02-28.** Fixed by the data, not chosen: the v2 labels are for users expiring
  around March 2017 and the transactions end 2017-03-31.
- **Population is `train_v2` as given**: 968,436 labelled users with history, churn 8.9%. A
  re-derived "February-active March expirers" was tested and rejected: it drops users with no
  February transaction, who churn at 65.8%.
- **The label is Kaggle's `is_churn`, as given.** Our recompute from transactions agrees for
  95.1%; the remainder is right-censoring at 2017-03-31, so a recompute would be a different,
  noisier label.
- **Churn is the only target.** Spend after the cutoff is deferred: 97.0% of renewers pay
  their last amount, so it is last price x not-churned and a regressor learns a price we
  already observe. Plan moves (2.5% of users) deferred; time-to-churn and cancel-after
  rejected. What would reopen this: data released beyond April 2017.

### 2. RevenueCat event mapping (decided, built: `agentstack.context.kkbox`)

| KKBox | RevenueCat | Rule |
|---|---|---|
| first transaction, registration known and within 30 days before it | `INITIAL_PURCHASE` | otherwise `RENEWAL` with `left_censored` |
| later non-cancel transaction | `RENEWAL` | |
| `is_cancel=1` row | `CANCELLATION` | auto-renew off, not a refund: it shortens the expiry in only 62% of cases, so the row's own expiry is carried |
| *(no row)* | `EXPIRATION` | derived: a renewal after the previous expiry, or a final expiry at or before the cutoff |
| zero paid on a priced plan | `period_type=TRIAL` | derived (no trial flag exists); else `NORMAL`; no `INTRO` |

- **Left-censoring is counted, not hidden.** 36% of users' first observed transaction is within
  30 days of registration; 49% registered before the data begins. On a real sample the mapping
  gives `INITIAL_PURCHASE` to 36.1% of users and marks 63.9% `left_censored`.
- **A lapse is strict.** A renewal the day after the expiry is an `EXPIRATION` then a
  `RENEWAL`. It is 14.8% of events on a real sample, because KKBox users often renew a day or
  more late. RevenueCat would hold a billing grace period before expiring; KKBox states none, so
  inventing one would be a modelled quantity. The gap stays recoverable from the two events, and
  Kaggle's own 30-day rule is a feature of it. *What would change it:* a documented grace period.
- **Expiry before the purchase date** (61,098 rows) is flagged `expiry_before_purchase` and never
  the source of an `EXPIRATION`.
- **Extras, not schema:** `payment_method_id`, plan length, list price, amount paid,
  `is_auto_renew`. Timestamps are UTC-midnight epoch ms; KKBox states no timezone.
- **Nothing after the cutoff is read**; an expiry after it is the future, not an event.
- Ordering follows the organisers' `WSDMChurnLabeller.scala` (same-day: subscribe before
  cancel), so the mapping is independent of row order.

### 3. The exploration is reproducible and local

`scripts/explore_kkbox.py` writes aggregates only (no user-level row, no `msno`);
`tests/live/test_kkbox_exploration.py` regenerates the report from the real files and it matches
byte for byte. No hosted call, no egress.

### 3b. One row per subscriber (decided, built: `kkbox.to_features`)

- Population is labelled users with any event at or before the cutoff; the label is joined by
  `msno` and never feeds a feature. Events after the cutoff are refused by the builder, so a
  caller that forgot the cutoff finds out instead of leaking.
- Registration after the cutoff is unknown at the cutoff (tenure null), not a negative tenure.
- `bd` outside 10-90 is null with `bd_missing` (60% of a real sample). Codes (`city`,
  `registered_via`, payment method) are categories, not ordered numbers; missing is a level.
- Total paid counts non-cancel rows only: a cancel row repeats the plan's amount, not a payment.
- `msno` and the raw dates are excluded with reasons (`kkbox.EXCLUDED`); the raw registration and
  expiry dates are candidates behind `raw_dates=True`, decided once in K13.
- Real sample: max |corr| with the label is 0.40, so the 0.9 rule excludes nothing yet.

### 4. Hosted scorer, placement and egress (decided in the spec; lands in K7-K10)

`HostedTabPFNScorer` implements the 4b `ChurnScorer` Protocol and lives in layer 7, the only
place `tabpfn_client` may be imported (contract 3 tightened). Fold assignment and encoding stay
ours so out-of-fold is checkable. Scoring is a **read**: no idempotency-ledger entry, but traced
and audited egress. The served model version is recorded or the scorer refuses.

#### 4a. Verified client surface (K7, `tabpfn-client` 0.6.1, read from the installed package)

- **Host:** `https://api.priorlabs.ai:443` (`server_config.yaml`). K8 pins it as a checked constant.
- **Token:** `TABPFN_TOKEN` in the environment or `set_access_token()`; `init()` never prompts. It
  raises if the server is unreachable or no valid token exists, which is K9's preflight signal.
- **Calls:** `TabPFNClassifier.fit(X, y)` then `predict_proba(X)`. The model is chosen by
  `model_path` (`"auto"` defers to the server), so the served version must be read back, not assumed (K8).
- **Limits** come from the server as `ModelLimit` (`train_set_max_rows`, `train_set_max_cells`,
  `test_set_max_rows`, `max_classes`, `max_cols`); they are measured, not hard-coded, in K12.
- **Server-side retention:** the client uploads train and test sets (`/upload/train_set/`) and has
  `get_data_summary`, `download_all_data` and `delete_all_datasets` endpoints, so uploaded cohort
  rows persist on the vendor's side until deleted. Checkpoint C must state this, and K8 should
  decide whether a scoring run deletes what it uploaded.
- **Second egress path: telemetry.** `tabpfn-common-utils` sends usage events to PostHog. It is on
  by default outside CI and is switched off with `TABPFN_DISABLE_TELEMETRY=1`. K9 sets that before
  the client is imported, and a fitness test pins it.
- **Dependency cost:** `tabpfn-client` pins `pandas<3`; the lock moved `pandas` 3.0.6 to 2.3.3 and
  patch-downgraded `pydantic`, `scikit-learn`, `tqdm` and `xxhash`. The full suite is green on it.
  `osv-scanner` on `uv.lock`: no issues.

### 5. Model-version provenance and replay (K8, K14)

The service chooses the checkpoint, so recorded scores (local, never committed) define a cohort;
a live call does not. A moving service cannot move a frozen cohort.

K8 built it as `execution/hosted_scorer.py` (layer 7; scoring is a read, so no ledger entry):

- **Recorded as `priorlabs:<billing_model_version>+<package_version>`**, both from the predict
  metadata (`_last_meta`, private to the client: it has no public accessor). If either is absent
  the scorer refuses, and a version that moves between folds is refused. Whether
  `billing_model_version` is the checkpoint that actually ran is **unverified until K12's live
  call**; the name suggests billing, and K12 must confirm or replace the field.
- **Host is pinned** to `https://api.priorlabs.ai:443`, read from what the client *will use*
  (`ServiceClient.base_url`), because `TABPFN_CLIENT_API_URL` can redirect it. Checked before any row
  is sent.
- **No retry of ours, no fallback.** Any failure is a `ScoringError`. The client library retries
  transport errors itself (it depends on `backoff`); that sits below this seam.
- **Upload deletion is not decided here.** Deleting what a run uploaded is a second egress call
  (`delete_all_datasets` would also remove data the account uploaded for other reasons), so it is
  left to Checkpoint C with the retention statement.
- `engine._encode` is now `engine.encode` (rename only), shared by both scorers.

### 6. Data egress (K11, Checkpoint C)

All engineered features are sent; `msno` and raw identifiers are not (modelling drops). Whether
the Kaggle competition rules permit third-party processing is **not verified by the author**;
the owner's statement is recorded here at Checkpoint C.

**K11 (built 2026-10-02): the declared cut.** `scripts/build_kkbox_cohort.py` writes
`data/kkbox-cohort.csv` offline. `kkbox.sample_users` draws `COHORT_USERS = 50,000` labelled
users, stratified on `is_churn`, seed `20170228`, independent of row order; users with no
history before the cutoff drop out afterwards, so the file has 49,863 rows, churn 8.9%
(`data_as_of kkbox-churn:7b2787de78c817e3`, recorded in `data/manifest.json`). The size is
**provisional**: K12 measures the hosted limits and the cut shrinks to fit them, never the folds.

### 7. Revenue and currency (decided, built: K1-K3, K6)

Observed `actual_amount_paid` of the last pre-cutoff transaction, annualised x12. Holds for the
30-day plan that 97% of last transactions use (942,378 of 968,436); the revenue note says so.
Currency is NTD, carried in a `currency` field and shown in the approval prompt; no FX
conversion, because a converted figure would be a modelled quantity.

Registered as `REGISTRY["kkbox-churn"]` with `derived_by="scripts/build_kkbox_cohort.py"`: the cohort
file is built, not downloaded (K11 recorded its `data_as_of`). The manifest test
exempts a derived cohort only until then; a downloaded cohort is never exempt.

## Consequences

- Sections 4-7 are decisions not yet built; this ADR is `proposed` until K16.
- One new dependency, `tabpfn-client` (approved 2026-10-01), arrives in K7.

### K9 amendment: contract 3 ignores two edges (approved by the owner, 2026-10-02)

Contract 3 is transitive, so no layer-1 module could reach `execution.hosted_scorer` (it holds
`tabpfn_client` and `urllib`). `worker_cli` and `preflight_cli` are where the hosted scorer is
built and proved at startup, so `.importlinter` contract 3 gains `ignore_imports` for exactly
those two edges. `test_layer_boundaries.py` pins the set. Rejected: moving the client into
`prediction` (contract 2 and 3 forbid it; scoring rows to a third party is egress) and a
composition root (a new architectural concept for two call sites).
