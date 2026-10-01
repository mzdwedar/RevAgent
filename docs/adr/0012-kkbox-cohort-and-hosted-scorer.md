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

### 5. Model-version provenance and replay (K8, K14)

The service chooses the checkpoint, so recorded scores (local, never committed) define a cohort;
a live call does not. A moving service cannot move a frozen cohort.

### 6. Data egress (K11, Checkpoint C)

All engineered features are sent; `msno` and raw identifiers are not (modelling drops). Whether
the Kaggle competition rules permit third-party processing is **not verified by the author**;
the owner's statement is recorded here at Checkpoint C.

### 7. Revenue and currency (K1-K3, K6)

Observed `actual_amount_paid` of the last pre-cutoff transaction, annualised x12. Holds for the
30-day plan that 97% of last transactions use (942,378 of 968,436); the revenue note says so.
Currency is NTD, carried in a `currency` field and shown in the approval prompt; no FX
conversion, because a converted figure would be a modelled quantity.

## Consequences

- Sections 4-7 are decisions not yet built; this ADR is `proposed` until K16.
- `tests/live` fails (does not skip) for KKBox until the cohort is built (K11).
- One new dependency, `tabpfn-client` (approved 2026-10-01), arrives in K7.
