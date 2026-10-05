"""Explore the KKBox raw files: what could be predicted, and how they map to RevenueCat events.

    uv run python scripts/explore_kkbox.py            # writes docs/evidence/kkbox-exploration.md

Read-only and local: no hosted call, no network, nothing written but the report. Outside
the package on purpose (it reads third-party files, like `fetch_datasets.py`).

The report holds **aggregates only**. The data is third-party and licensed, so no user-level
row and no `msno` ever reaches a committed file.

The pure functions (`derive_targets`, `last_pre_cutoff`) are what `tests/fitness` checks on a
tiny fixture: every candidate target is computed from transactions strictly *after* the
cutoff, every feature-side summary from transactions at or before it, and the two never
share a row. A target that peeked at the feature window would measure the label it was
built from.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from agentstack.context.kkbox import CUTOFF, ordered, to_date

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "kkbox-raw"
REPORT = ROOT / "docs" / "evidence" / "kkbox-exploration.md"

# The v2 labels are for users whose subscription expires in March 2017, scored from history
# up to the end of February. Evidence: 95.7% of labelled users transact in February, and 89.5%
# of those have a last expiry in March (see the report). `WSDMChurnLabeller.scala`, the
# organisers' own labeller, uses the same shape one month earlier (cutoff 2017-01-31).
WINDOW_START = 20170201
LABEL_WINDOW = (20170301, 20170331)
DATA_END = 20170331

TX_COLUMNS = [
    "msno",
    "payment_method_id",
    "payment_plan_days",
    "plan_list_price",
    "actual_amount_paid",
    "is_auto_renew",
    "transaction_date",
    "membership_expire_date",
    "is_cancel",
]


def last_pre_cutoff(
    tx: pd.DataFrame, cutoff: int = CUTOFF, since: int = WINDOW_START
) -> pd.DataFrame:
    """One row per user: their last transaction in [since, cutoff]. Feature side only."""
    window = tx[(tx.transaction_date >= since) & (tx.transaction_date <= cutoff)]
    return ordered(window).groupby("msno").tail(1).set_index("msno")


def derive_targets(tx: pd.DataFrame, last: pd.DataFrame, cutoff: int = CUTOFF) -> pd.DataFrame:
    """Candidate targets, one row per user in `last`, from transactions **after** the cutoff.

    `spend_after`        paid amount of non-cancel transactions after the cutoff (observed to
                         DATA_END only: right-censored for late-window expirers)
    `cancelled_after`    any cancel row after the cutoff
    `renewed_after`      any non-cancel transaction after the cutoff
    `plan_move`          first renewal compared with the last pre-cutoff plan:
                         up / down / same by list price, `none` if no renewal
    """
    after = tx[(tx.transaction_date > cutoff) & tx.msno.isin(last.index)]
    paid = after[after.is_cancel == 0]
    out = pd.DataFrame(index=last.index)
    out["spend_after"] = paid.groupby("msno").actual_amount_paid.sum().reindex(out.index).fillna(0)
    out["cancelled_after"] = (
        after[after.is_cancel == 1].groupby("msno").size().reindex(out.index).fillna(0) > 0
    )
    out["renewed_after"] = paid.groupby("msno").size().reindex(out.index).fillna(0) > 0
    first = ordered(paid).groupby("msno").head(1).set_index("msno").plan_list_price
    delta = (first.reindex(out.index) - last.plan_list_price).where(
        first.reindex(out.index).notna()
    )
    first_paid = ordered(paid).groupby("msno").head(1).set_index("msno").actual_amount_paid
    out["first_paid_after"] = first_paid.reindex(out.index)
    out["plan_move"] = np.select([delta > 0, delta < 0, delta == 0], ["up", "down", "same"], "none")
    return out


def renewal_gap(tx: pd.DataFrame, last: pd.DataFrame, cutoff: int = CUTOFF) -> pd.Series:
    """The organisers' `calculateRenewalGap`: days from the (possibly shortened) last expiry to
    the first renewal after the cutoff; 9999 if there is none. Used only to check how far the
    Kaggle label can be reproduced from the transactions we have."""
    after = ordered(tx[(tx.transaction_date > cutoff) & tx.msno.isin(last.index)])
    expiry = to_date(last.membership_expire_date).to_dict()
    gaps: dict[str, int] = {}
    for msno, group in after.groupby("msno", sort=False):
        end = expiry[msno]
        gaps[str(msno)] = 9999
        for date, expires, cancel in zip(
            to_date(group.transaction_date),
            to_date(group.membership_expire_date),
            group.is_cancel,
            strict=True,
        ):
            if cancel == 1:
                end = min(end, expires)
            else:
                gaps[str(msno)] = (date - end).days
                break
    return pd.Series(gaps).reindex(last.index).fillna(9999).astype(int)


# ---------------------------------------------------------------------------- loading


def load_history(root: Path, labelled: set[str]) -> pd.DataFrame:
    """v1 history (to 2017-02-28) for the labelled users, plus v2's rows for them.

    v2 alone is not history: 1.07M of its 1.43M rows are dated March 2017, the label window.
    """
    cache = root / "_labelled_history.pkl"
    if cache.exists():
        cached: pd.DataFrame = pd.read_pickle(cache)
        return cached
    parts = [
        chunk[chunk.msno.isin(labelled)]
        for chunk in pd.read_csv(root / "transactions.csv", chunksize=2_000_000)
    ]
    v2 = pd.read_csv(root / "transactions_v2.csv")
    both = pd.concat([*parts, v2[v2.msno.isin(labelled)]], ignore_index=True).drop_duplicates()
    both.to_pickle(cache)
    return both


# ---------------------------------------------------------------------------- report


def table(frame: pd.DataFrame) -> str:
    cols = [str(c) for c in frame.columns]
    head = "| " + " | ".join([str(frame.index.name or "")] + cols) + " |"
    rule = "|" + "|".join(["---"] * (len(cols) + 1)) + "|"
    rows = [
        "| "
        + " | ".join(
            [str(i)]
            + [
                f"{v:,.4g}"
                if isinstance(v, float)
                else f"{v:,}"
                if isinstance(v, int | np.integer)
                else str(v)
                for v in r
            ]
        )
        + " |"
        for i, r in zip(frame.index, frame.itertuples(index=False), strict=True)
    ]
    return "\n".join([head, rule, *rows])


def explore(root: Path = RAW) -> dict[str, Any]:
    """Every number the report states, computed once."""
    labels = pd.read_csv(root / "train_v2.csv")
    members = pd.read_csv(root / "members_v3.csv")
    history = load_history(root, set(labels.msno))
    n: dict[str, Any] = {}

    n["labelled"] = len(labels)
    n["churn_rate"] = round(float(labels.is_churn.mean()), 4)
    n["v1_users_covered"] = int(history[history.transaction_date <= CUTOFF].msno.nunique())
    pre = history[history.transaction_date <= CUTOFF]
    per_user = pre.groupby("msno").size()
    n["tx_per_user_median"] = float(per_user.median())
    n["tx_per_user_p10"] = float(per_user.quantile(0.1))
    v2 = pd.read_csv(root / "transactions_v2.csv", usecols=["transaction_date"])
    n["v2_rows"] = len(v2)
    n["v2_rows_in_march"] = int((v2.transaction_date // 100 == 201703).sum())

    # The population question: re-deriving "February-active March expirers" drops users that
    # the Kaggle label includes, so measure what is dropped before choosing.
    last_feb = last_pre_cutoff(history)
    in_feb = labels.msno.isin(last_feb.index)
    n["feb_active_share"] = round(float(in_feb.mean()), 4)
    n["churn_feb_active"] = round(float(labels[in_feb].is_churn.mean()), 4)
    n["churn_not_feb_active"] = round(float(labels[~in_feb].is_churn.mean()), 4)
    feb_march = last_feb.reindex(labels.msno[in_feb]).membership_expire_date // 100 == 201703
    n["feb_active_expiring_in_march_share"] = round(float(feb_march.mean()), 4)

    last = last_pre_cutoff(history, since=0)
    pop = labels.merge(last, left_on="msno", right_index=True, how="inner").set_index("msno")
    n["population"] = len(pop)
    n["population_share"] = round(len(pop) / len(labels), 4)
    n["population_churn_rate"] = round(float(pop.is_churn.mean()), 4)
    expiry = pop.membership_expire_date
    bucket = pd.Series(
        np.select(
            [expiry < LABEL_WINDOW[0], expiry <= LABEL_WINDOW[1] - 7, expiry <= LABEL_WINDOW[1]],
            ["before_march", "march_to_24th", "march_25th_on"],
            "after_march",
        ),
        index=pop.index,
        name="last_expiry",
    )
    late = bucket == "march_25th_on"
    n["population_expiring_last_week_share"] = round(float(late.mean()), 4)
    n["churn_by_expiry_bucket"] = pop.groupby(bucket).is_churn.agg(["mean", "size"]).round(4)

    gap = renewal_gap(history, last.loc[pop.index])
    agree = (gap >= 30).astype(int) == pop.is_churn
    n["label_reproduction"] = round(float(agree.mean()), 4)
    n["label_agreement_by_bucket"] = agree.groupby(bucket).mean().round(4).to_dict()
    kinds = np.select([gap <= 0, gap < 30, gap < 9999], ["by_expiry", "1_to_29", "30_plus"], "none")
    n["renewal_timing_shares"] = pd.Series(kinds).value_counts(normalize=True).round(4).to_dict()
    targets = derive_targets(history, last.loc[pop.index])
    joined = pop[["is_churn"]].join(targets)
    n["renewed_after_share"] = round(float(joined.renewed_after.mean()), 4)
    n["cancelled_after_share"] = round(float(joined.cancelled_after.mean()), 4)
    n["spend_zero_share"] = round(float((joined.spend_after == 0).mean()), 4)
    n["spend_mean_payers"] = round(float(joined.spend_after[joined.spend_after > 0].mean()), 1)
    n["churn_by_renewed"] = joined.groupby("renewed_after").is_churn.mean().round(4).to_dict()
    n["churn_by_plan_move"] = joined.groupby("plan_move").is_churn.agg(["mean", "size"]).round(4)
    n["spend_vs_churn_spearman"] = round(
        float(joined.spend_after.corr(joined.is_churn, method="spearman")), 4
    )
    n["spend_when_not_churn_zero_share"] = round(
        float((joined[joined.is_churn == 0].spend_after == 0).mean()), 4
    )
    n["plan_move_shares"] = joined.plan_move.value_counts(normalize=True).round(4).to_dict()
    n["spend_late_zero_share"] = round(float((joined[late].spend_after == 0).mean()), 4)
    n["spend_early_zero_share"] = round(float((joined[~late].spend_after == 0).mean()), 4)
    renewers = joined[joined.renewed_after]
    same_paid = renewers.first_paid_after == last.loc[renewers.index].actual_amount_paid
    n["renewer_pays_last_amount_share"] = round(float(same_paid.mean()), 4)
    n["renewer_spend_vs_last_paid_corr"] = round(
        float(renewers.first_paid_after.corr(last.loc[renewers.index].actual_amount_paid)), 4
    )

    # Feature-side facts
    feat = pop.copy()
    feat["auto_renew_off"] = feat.is_auto_renew == 0
    n["churn_by_auto_renew"] = feat.groupby("is_auto_renew").is_churn.agg(["mean", "size"]).round(4)
    n["churn_by_last_is_cancel"] = feat.groupby("is_cancel").is_churn.agg(["mean", "size"]).round(4)
    n["churn_by_plan_days"] = (
        feat.groupby("payment_plan_days")
        .is_churn.agg(["mean", "size"])
        .sort_values("size", ascending=False)
        .head(6)
        .round(4)
    )
    numeric = [
        "payment_plan_days",
        "plan_list_price",
        "actual_amount_paid",
        "is_auto_renew",
        "is_cancel",
        "payment_method_id",
    ]
    corr = feat[numeric].corrwith(feat.is_churn).abs().round(4).sort_values(ascending=False)
    n["max_abs_corr_last_tx"] = corr.to_dict()
    n["zero_paid_share"] = round(float((pop.actual_amount_paid == 0).mean()), 4)
    n["payment_methods"] = int(history.payment_method_id.nunique())

    # RevenueCat-mapping facts, over the population's pre-cutoff history
    hist = ordered(pre[pre.msno.isin(pop.index)])
    first = hist.groupby("msno").head(1).set_index("msno")
    mb = members.set_index("msno")
    reg = to_date(mb.registration_init_time).reindex(first.index)
    first_date = to_date(first.transaction_date)
    n["members_coverage"] = round(float(pop.index.isin(mb.index).mean()), 4)
    gap_days = (first_date - reg).dt.days
    n["registered_before_window_share"] = round(float((reg < pd.Timestamp("2015-01-01")).mean()), 4)
    n["first_tx_within_30d_of_registration"] = round(
        float(((gap_days >= 0) & (gap_days <= 30)).mean()), 4
    )
    n["first_tx_registration_unknown_share"] = round(float(reg.isna().mean()), 4)
    n["cancel_rows_share"] = round(float((hist.is_cancel == 1).mean()), 4)
    n["users_with_any_cancel"] = round(
        float(hist[hist.is_cancel == 1].msno.nunique() / hist.msno.nunique()), 4
    )
    n["zero_paid_rows_share"] = round(float((hist.actual_amount_paid == 0).mean()), 4)
    n["zero_paid_priced_share"] = round(
        float(((hist.actual_amount_paid == 0) & (hist.plan_list_price > 0)).mean()), 4
    )
    dup_day = hist.groupby(["msno", "transaction_date"]).size()
    n["same_day_multi_tx_share"] = round(float((dup_day > 1).mean()), 4)
    prev_exp = hist.groupby("msno").membership_expire_date.shift()
    n["cancel_shortens_expiry_share"] = round(
        float(
            (
                hist[hist.is_cancel == 1].membership_expire_date < prev_exp[hist.is_cancel == 1]
            ).mean()
        ),
        4,
    )
    n["expire_before_transaction_rows"] = int(
        (hist.membership_expire_date < hist.transaction_date).sum()
    )

    bd = mb.bd.reindex(pop.index)
    n["bd_valid_share"] = round(float(((bd >= 10) & (bd <= 90)).mean()), 4)
    n["bd_zero_share"] = round(float((bd == 0).mean()), 4)
    n["gender_missing_share"] = round(float(mb.gender.reindex(pop.index).isna().mean()), 4)
    n["registration_year_range"] = [int(reg.dt.year.min()), int(reg.dt.year.max())]
    return n


def render(n: dict[str, Any]) -> str:
    def pct(key: str) -> str:
        return f"{float(n[key]) * 100:.1f}%"

    lines = [
        "# KKBox exploration",
        "",
        "Generated by `scripts/explore_kkbox.py` from the Kaggle",
        "`kkbox-churn-prediction-challenge` files (`train_v2`, `transactions_v2`, `transactions`",
        "v1, `members_v3`). **Aggregates only**: the data is third-party and licensed. Local,",
        f"read-only, no hosted call. Cutoff {CUTOFF}. Numbers below are computed; the reading of",
        "them is in `kkbox-exploration-reading.md`, which a regeneration leaves alone.",
        "",
        "## 1. What the files are",
        "",
        f"- `train_v2.csv`: {n['labelled']:,} labelled users, churn rate {pct('churn_rate')}.",
        f"- `transactions_v2.csv`: {n['v2_rows']:,} rows, of which {n['v2_rows_in_march']:,} are",
        "  dated March 2017 (the label window).",
        f"- v1 `transactions.csv` (to 2017-02-28) covers {n['v1_users_covered']:,} labelled users;",
        f"  transactions per user up to the cutoff: median {n['tx_per_user_median']:.0f},",
        f"  p10 {n['tx_per_user_p10']:.0f}.",
        f"- {pct('feb_active_share')} of labelled users have a February transaction (churn",
        f"  {pct('churn_feb_active')}); the rest churn at {pct('churn_not_feb_active')}. Of the",
        f"  February-active, {pct('feb_active_expiring_in_march_share')} have a last expiry in",
        "  March.",
        "- Population used below: all labelled users with any transaction up to the cutoff:",
        f"  {n['population']:,} ({pct('population_share')} of labelled), churn",
        f"  {pct('population_churn_rate')}. Churn by last expiry:",
        "",
        table(n["churn_by_expiry_bucket"]),
        "",
        f"- Transactions stop {DATA_END}; {pct('population_expiring_last_week_share')} of the",
        "  population expires in the last week of March.",
        "- Our recompute of `is_churn` from transactions (organisers' algorithm) agrees with",
        f"  the Kaggle label for {pct('label_reproduction')} of the population; by last expiry:",
        f"  {n['label_agreement_by_bucket']}.",
        f"- Renewal timing (gap from expiry to first renewal): {n['renewal_timing_shares']}.",
        "",
        "## 2. Candidate targets (all computed from transactions after the cutoff)",
        "",
        f"- Renewed after the cutoff: {pct('renewed_after_share')}. Cancel row after the cutoff:",
        f"  {pct('cancelled_after_share')}.",
        f"- Spend after the cutoff is zero for {pct('spend_zero_share')}; mean among payers",
        f"  {n['spend_mean_payers']} NTD.",
        "- Zero spend among users who did **not** churn:",
        f"  {pct('spend_when_not_churn_zero_share')}.",
        f"- Zero spend if expiring before the 25th: {pct('spend_early_zero_share')}; from the",
        f"  25th on: {pct('spend_late_zero_share')}.",
        "- Renewers whose first payment equals their last pre-cutoff payment:",
        f"  {pct('renewer_pays_last_amount_share')} (correlation",
        f"  {n['renewer_spend_vs_last_paid_corr']}).",
        f"- Spearman(spend after, `is_churn`): {n['spend_vs_churn_spearman']}.",
        f"- Plan move among all: {n['plan_move_shares']}. Churn rate by move:",
        "",
        table(n["churn_by_plan_move"]),
        "",
        "## 3. Churn by pre-cutoff state (feature side)",
        "",
        "Auto-renew at the last pre-cutoff transaction:",
        "",
        table(n["churn_by_auto_renew"]),
        "",
        "Last pre-cutoff transaction is a cancel:",
        "",
        table(n["churn_by_last_is_cancel"]),
        "",
        "Most common plan lengths:",
        "",
        table(n["churn_by_plan_days"]),
        "",
        f"|corr| with `is_churn` (leakage bar 0.9): {n['max_abs_corr_last_tx']}.",
        "",
        "## 4. Facts for the RevenueCat mapping (population's pre-cutoff history)",
        "",
        f"- Cancel rows: {pct('cancel_rows_share')} of rows; {pct('users_with_any_cancel')} of",
        "  users have one. A cancel row shortens the expiry in",
        f"  {pct('cancel_shortens_expiry_share')} of cases.",
        "- First observed transaction within 30 days of registration:",
        f"  {pct('first_tx_within_30d_of_registration')}; registered before 2015-01-01:",
        f"  {pct('registered_before_window_share')}; members row present:",
        f"  {pct('members_coverage')}.",
        f"- Zero-paid rows: {pct('zero_paid_rows_share')}; zero paid on a priced plan:",
        f"  {pct('zero_paid_priced_share')}.",
        f"- User-days with more than one transaction: {pct('same_day_multi_tx_share')}.",
        f"- Rows with expiry before the transaction date: {n['expire_before_transaction_rows']:,}.",
        f"- `payment_method_id` levels: {n['payment_methods']}.",
        f"- `bd` in 10-90: {pct('bd_valid_share')}; `bd` zero: {pct('bd_zero_share')}; gender",
        f"  missing: {pct('gender_missing_share')}; registration years",
        f"  {n['registration_year_range']}.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=RAW)
    parser.add_argument("--out", type=Path, default=REPORT)
    args = parser.parse_args()
    numbers = explore(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(numbers))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
