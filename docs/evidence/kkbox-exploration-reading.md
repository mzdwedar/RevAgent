# KKBox exploration: reading

Hand-written, against the numbers in `kkbox-exploration.md` (regenerate with
`uv run python scripts/explore_kkbox.py`). If those numbers move, this file is stale and says so
here: **written 2026-10-01 against the files as downloaded that day.**

## What the data is, which the spec got wrong

1. **`transactions_v2.csv` is not history.** 1.07M of its 1.43M rows are March 2017, the label
   window. History comes from v1 `transactions.csv` (21.5M rows, to 2017-02-28): a median of 16
   transactions per labelled user, 99.7% of labelled users covered. The spec assumed the v2
   files only. **Spec amendment needed:** add `transactions.csv` (1.7 GB extracted).
2. **The cutoff is 2017-02-28**, not something to choose. The v2 labels are for users expiring
   around March 2017 and the data ends 2017-03-31.
3. **The population is `train_v2.csv` as given** (968,436 users with history, churn 8.9%). My first
   attempt to re-derive "February-active March expirers" is wrong: it drops the 4.3% of labelled
   users with no February transaction, who churn at **65.8%**, and would report 5.2% churn. The
   spec's decision 7 ("expiry in the label window") is satisfied by the label set itself; we do
   not re-derive it. Users already lapsed before March (2.1% of the population) churn at 43.8%.
4. **The label reproduces at 95.1%** from transactions with the organisers' algorithm. Use
   Kaggle's `is_churn` as given; do not recompute it.

## Candidate targets

| Target | Reading | Recommendation |
|---|---|---|
| `is_churn` | Binary, 8.9%, strongly structured (auto-renew off: 41% vs 4.5%) | **Ship** |
| Spend after cutoff (revenue) | Renewers pay their last amount in **97.0%** of cases (corr 0.74); 94% stay on the same plan. Spend is close to *last price × did-not-churn*, so a regressor learns the price we already observe and adds little beyond churn. Predicted revenue is also a modelled quantity and can never be the value-at-risk floor. | **Defer** (analytic value at risk stays `churn probability × observed ARPU`, which the system already does) |
| Time to churn / renewal gap | 76% renew by expiry, 18% within 29 days, 0.14% at 30+ days, 5.9% never in the window: the gap is nearly degenerate, and the 30+ tail is censored by the data ending 2017-03-31. | **Reject** with this release |
| Plan move on renewal | Up/down is 2.5% of users; defined only for renewers. | **Defer** (too rare to learn from at this size) |
| Cancel after cutoff | 3.1%; mostly a symptom of churn. | **Reject** as a target; keep `is_cancel` as a feature |

The honest summary: **KKBox, as released, supports one useful target, churn.** The more
interesting ones (revenue, tenure, plan moves) are either arithmetic on observed price, censored,
or too rare. A later release with data beyond April 2017 would change this.

## What this changes for the mapping to RevenueCat's schema

- `CANCELLATION` = an `is_cancel=1` row (1.6% of rows, 20.8% of users). It shortens the expiry in
  only 62.1% of cases, so it is "auto-renew turned off", not a refund; do not infer the expiry
  change from the event.
- `EXPIRATION` is not a row: it is derived (the expiry passes with no later transaction).
- `INITIAL_PURCHASE` is unreliable: only 36.1% of users' first observed transaction is within 30
  days of registration and 49.3% registered before the data begins (2015-01-01). The mapping
  rule (first transaction within the window of registration, otherwise `RENEWAL`) holds, and the
  censored share is large enough that it must be reported, not hidden.
- 61,098 rows have an expiry **before** the transaction date: they must be handled explicitly
  (a data quirk, not an expiry event).
- `period_type=TRIAL` is derived and rare (0.4% zero-paid on a priced plan). Reasonable, and
  small enough not to matter for the model.
- Demographics are thin: `bd` is plausible for 39.8%, zero for 48.8%; gender missing for 59.9%;
  the members row is missing for 11.3%. Every demographic feature carries a missing flag.
- Non-30-day plans (410, 195, 180, 7, 90 days) churn at 79-93% against 6.8% for 30-day plans:
  plan length is a strong feature, and the `periods per year = 12` revenue assumption holds for
  98% of rows (942,378 of 968,436 last transactions), which the revenue note should state.

## Decisions this needs from you (Checkpoint E)

1. **Ship `is_churn` only**; defer revenue and plan-move; reject time-to-churn and cancel. Agree?
2. **Population = `train_v2` as given, cutoff 2017-02-28.** Agree?
3. **Spec amendment: add `transactions.csv` (v1) as a raw input** (1.7 GB extracted, gitignored).
