"""KKBox transactions, re-expressed as RevenueCat webhook events (Context, retrieval, memory).

The cohort should speak the schema the agent will see in production, so KKBox's one-row-per-
transaction table becomes events: `INITIAL_PURCHASE`, `RENEWAL`, `CANCELLATION`,
`EXPIRATION`, each with `period_type`, `purchased_at_ms`, `expiration_at_ms` and
`app_user_id`. Fields RevenueCat has no name for (`payment_method_id`, the plan length, the
amounts) travel as **extras**, never forced into a schema field.

What the data says, measured on the real files (`docs/evidence/kkbox-exploration.md`), and what
each rule below does about it:

* A cancel row (`is_cancel=1`) is *auto-renew turned off*, not a refund: it shortens the expiry
  in only 62% of cases. It is `CANCELLATION`, and the expiry it carries is its own.
* **No row is an expiry.** `EXPIRATION` is derived, from the expiry passing with no renewal
  before it, and never from after the cutoff.
* **`INITIAL_PURCHASE` is unreliable.** Only 36% of users' first observed transaction is within
  30 days of registration, and 49% registered before the data begins. A first transaction is an
  initial purchase only when registration is known and within `INITIAL_WINDOW_DAYS` before it.
  Otherwise it is a `RENEWAL` flagged `left_censored`, so the censoring is counted, not hidden.
* 61,098 rows carry an expiry *before* their own transaction date. That is a data quirk, not an
  expiry: the row is flagged `expiry_before_purchase` and is never the source of an
  `EXPIRATION`.
* KKBox has no trial flag. `period_type` is `TRIAL` for a zero payment on a priced plan,
  `NORMAL` otherwise. Derived, never observed; there is no `INTRO`.

Cutoff: nothing after it is read. A transaction after the cutoff produces no event, and an
expiry after the cutoff produces no `EXPIRATION`: the label is decided by what happens next.

`to_features` then collapses the events to one row per subscriber at the cutoff, joined to
Kaggle's `is_churn`. It reads only events at or before the cutoff and the members table, so a
later transaction cannot move a feature. The raw registration and expiry dates can encode the
label window; they are *candidates*, off unless asked for (decided once, in K13).

This module computes; it reads no file and reaches no network (fetching is
`scripts/fetch_datasets.py`, building is `scripts/build_kkbox_cohort.py`).
"""

from __future__ import annotations

from typing import Final

import numpy as np
import pandas as pd

# The v2 labels are for users expiring around March 2017, scored from history to the end of
# February. Fixed by the data, not chosen (docs/evidence/kkbox-exploration-reading.md).
CUTOFF: Final = 20170228

# The declared cut: how many labelled users are scored. Provisional until K12 measures the hosted
# limits (rows per fit, quota); the cut shrinks to fit them, the folds never do (ADR-0012).
COHORT_USERS: Final = 50_000
COHORT_SEED: Final = 20170228

# A first observed transaction this soon after registration is read as an initial purchase.
INITIAL_WINDOW_DAYS: Final = 30

EVENT_TYPES: Final = ("INITIAL_PURCHASE", "RENEWAL", "CANCELLATION", "EXPIRATION")

# The RevenueCat webhook fields we fill, and the extras we carry beside them.
RC_COLUMNS: Final = (
    "type",
    "app_user_id",
    "period_type",
    "event_timestamp_ms",
    "purchased_at_ms",
    "expiration_at_ms",
)
EXTRA_COLUMNS: Final = (
    "payment_method_id",
    "payment_plan_days",
    "plan_list_price",
    "actual_amount_paid",
    "is_auto_renew",
    "left_censored",
    "expiry_before_purchase",
)

# Not features, with the reason each is out (recorded so the drop is a decision, not an omission).
EXCLUDED: Final = {
    "msno": "identifier: names the row; a hash carries no signal and adds memorisation risk",
    "registration_init_time": "raw date: a candidate (registration_date_raw), off unless asked",
    "membership_expire_date": "raw date: a candidate (expiry_date_raw), off unless asked",
    "transaction_date": "raw date: tenure and recency carry what it knows without naming a day",
}
CANDIDATE_COLUMNS: Final = ("registration_date_raw", "expiry_date_raw")
TARGET: Final = "is_churn"

# A candidate column is kept only if it passes all three (K13): known at the cutoff, not an
# identifier, and not correlated with the label like an outcome (the bar `tests/live` enforces).
IDENTIFIER_SHARE: Final = 0.5
MAX_LABEL_CORRELATION: Final = 0.9

# `bd` outside this range is a data-entry artefact (zero for most users), not an age.
BD_RANGE: Final = (10, 90)
_DAY_MS: Final = 86_400_000

_EPOCH = pd.Timestamp("1970-01-01")
_ORDER = {name: position for position, name in enumerate(EVENT_TYPES)}


def to_date(series: pd.Series) -> pd.Series:
    """KKBox's `YYYYMMDD` integers as dates; an impossible date is NaT, not an error."""
    return pd.to_datetime(series.astype("int64").astype(str), format="%Y%m%d", errors="coerce")


def to_ms(series: pd.Series) -> pd.Series:
    """`YYYYMMDD` integers as UTC-midnight epoch milliseconds (KKBox states no timezone)."""
    return ((to_date(series) - _EPOCH) // pd.Timedelta(milliseconds=1)).astype("Int64")


def ordered(tx: pd.DataFrame) -> pd.DataFrame:
    """Transactions in the organisers' order (`WSDMChurnLabeller.scala`): by date, then, on the
    same day, plan signature descending, subscribe before cancel, renewals extending the expiry
    and cancels shortening it."""
    keyed = tx.assign(
        sig=tx.plan_list_price.astype(str)
        + tx.payment_plan_days.astype(str)
        + tx.payment_method_id.astype(str),
        tie=np.where(tx.is_cancel == 0, tx.membership_expire_date, -tx.membership_expire_date),
    )
    return keyed.sort_values(
        ["msno", "transaction_date", "sig", "is_cancel", "tie"],
        ascending=[True, True, False, True, True],
        kind="stable",
    ).drop(columns=["sig", "tie"])


def to_events(
    transactions: pd.DataFrame,
    registration: pd.Series,
    cutoff: int = CUTOFF,
) -> pd.DataFrame:
    """One row per RevenueCat event, for every transaction at or before `cutoff`.

    `registration` maps `msno` to the `registration_init_time` integer; a user it does not name
    has an unknown registration, so their first observed transaction is a censored `RENEWAL`.
    """
    history = ordered(transactions[transactions.transaction_date <= cutoff]).reset_index(drop=True)
    user = history.groupby("msno")
    history["first"] = user.cumcount() == 0
    history["valid_expiry"] = history.membership_expire_date >= history.transaction_date
    previous = {
        name: user[name].shift()
        for name in (
            "membership_expire_date",
            "transaction_date",
            "valid_expiry",
            "payment_method_id",
            "payment_plan_days",
            "plan_list_price",
            "actual_amount_paid",
            "is_auto_renew",
        )
    }

    known = history.msno.map(registration)
    days_since = (to_date(history.transaction_date) - to_date(known.fillna(0))).dt.days
    initial = history["first"] & (history.is_cancel == 0) & known.notna()
    initial &= (days_since >= 0) & (days_since <= INITIAL_WINDOW_DAYS)
    censored = history["first"] & ~initial

    own = history.assign(
        type=np.select(
            [history.is_cancel == 1, initial], ["CANCELLATION", "INITIAL_PURCHASE"], "RENEWAL"
        ),
        event_date=history.transaction_date,
        purchased_date=history.transaction_date,
        expiry_date=history.membership_expire_date,
        left_censored=censored,
        expiry_before_purchase=~history.valid_expiry,
    )

    # A lapse: a renewal that arrives after the previous row's expiry. The expiry passed, so
    # the subscription expired then, and the renewal is a resubscription.
    lapsed = (
        ~history["first"]
        & (history.is_cancel == 0)
        & previous["valid_expiry"].fillna(False).astype(bool)
        & (history.transaction_date > previous["membership_expire_date"])
    )
    lapse = history[lapsed].assign(
        type="EXPIRATION",
        event_date=previous["membership_expire_date"][lapsed],
        purchased_date=previous["transaction_date"][lapsed],
        expiry_date=previous["membership_expire_date"][lapsed],
        left_censored=False,
        expiry_before_purchase=False,
        payment_method_id=previous["payment_method_id"][lapsed],
        payment_plan_days=previous["payment_plan_days"][lapsed],
        plan_list_price=previous["plan_list_price"][lapsed],
        actual_amount_paid=previous["actual_amount_paid"][lapsed],
        is_auto_renew=previous["is_auto_renew"][lapsed],
    )

    # The end of the record: the last row's expiry has passed by the cutoff and nothing came
    # after it. An expiry after the cutoff is the future, and is not an event yet.
    last = user.cumcount(ascending=False) == 0
    ended = last & history.valid_expiry & (history.membership_expire_date <= cutoff)
    final = history[ended].assign(
        type="EXPIRATION",
        event_date=history.membership_expire_date[ended],
        purchased_date=history.transaction_date[ended],
        expiry_date=history.membership_expire_date[ended],
        left_censored=False,
        expiry_before_purchase=False,
    )

    events = pd.concat([own, lapse, final], ignore_index=True)
    period = np.where(
        (events.actual_amount_paid == 0) & (events.plan_list_price > 0), "TRIAL", "NORMAL"
    )
    out = pd.DataFrame(
        {
            "type": events.type,
            "app_user_id": events.msno,
            "period_type": period,
            "event_timestamp_ms": to_ms(events.event_date),
            "purchased_at_ms": to_ms(events.purchased_date),
            "expiration_at_ms": to_ms(events.expiry_date),
            "payment_method_id": events.payment_method_id,
            "payment_plan_days": events.payment_plan_days,
            "plan_list_price": events.plan_list_price,
            "actual_amount_paid": events.actual_amount_paid,
            "is_auto_renew": events.is_auto_renew,
            "left_censored": events.left_censored.astype(bool),
            "expiry_before_purchase": events.expiry_before_purchase.astype(bool),
        }
    )
    out["_order"] = out.type.map(_ORDER)
    out = out.sort_values(["app_user_id", "event_timestamp_ms", "_order"], kind="stable").drop(
        columns="_order"
    )
    return out.reset_index(drop=True)


def sample_users(labels: pd.DataFrame, n: int, *, seed: int) -> pd.Series:
    """The declared cut: `n` users drawn from `labels`, stratified on the label, sorted by `msno`.

    Pure and seeded: the same labels and seed give the same users whatever the row order, so the
    cohort is a reproducible population rather than a lucky draw. Each class keeps its share
    (at least one user, so a rare class survives); a cut at or above the population is the whole
    population. It chooses *who*, from the label alone, and is never widened to suit a target.
    """
    if n <= 0:
        raise ValueError("the cut must be a positive number of users")
    if not labels.msno.is_unique:
        raise ValueError("labels must have one row per msno")
    ordered_labels = labels.sort_values("msno")
    if n >= len(ordered_labels):
        return ordered_labels.msno.reset_index(drop=True)

    rng = np.random.default_rng(seed)
    chosen = []
    for _, group in ordered_labels.groupby(TARGET, sort=True):
        take = max(1, round(n * len(group) / len(ordered_labels)))
        chosen.append(group.msno.iloc[np.sort(rng.choice(len(group), size=take, replace=False))])
    return pd.concat(chosen).sort_values().reset_index(drop=True)


def candidate_refusals(frame: pd.DataFrame, cutoff: int = CUTOFF) -> dict[str, str]:
    """Why each raw-date candidate present in `frame` may not be kept; empty means all pass.

    Registration must not be after the cutoff (the builder nulls those, so one here means a
    caller bypassed it). A candidate that is distinct for about every row names the row, and one
    correlated with `is_churn` above `MAX_LABEL_CORRELATION` is the label in another form.
    """
    refused: dict[str, str] = {}
    for column in CANDIDATE_COLUMNS:
        if column not in frame:
            continue
        values = frame[column].dropna()
        if column == "registration_date_raw" and (values > cutoff).any():
            refused[column] = f"known only after the cutoff {cutoff}: a registration after it"
        elif len(values) and values.nunique() / len(values) > IDENTIFIER_SHARE:
            refused[column] = "identifier: distinct for most rows, so it names the row"
        elif abs(frame[column].corr(frame[TARGET])) > MAX_LABEL_CORRELATION:
            refused[column] = f"|corr| with {TARGET} above {MAX_LABEL_CORRELATION}"
    return refused


def _level(series: pd.Series) -> pd.Series:
    """An integer code as a category (never an ordered number); missing is its own level."""
    return series.astype("Int64").astype("string").fillna("unknown").astype(object)


def to_features(
    events: pd.DataFrame,
    members: pd.DataFrame,
    labels: pd.DataFrame,
    cutoff: int = CUTOFF,
    *,
    raw_dates: bool = False,
) -> pd.DataFrame:
    """One row per labelled subscriber with history, indexed by `msno`, plus `is_churn`.

    `events` is `to_events` output (nothing after `cutoff`: a later event is refused, not
    ignored, so a caller that forgot the cutoff finds out). `members` has `msno`, `city`, `bd`,
    `gender`, `registered_via`, `registration_init_time`; `labels` has `msno`, `is_churn`.
    A labelled user with no event has no history to score and is left out.
    """
    cutoff_ms = int(to_ms(pd.Series([cutoff])).iloc[0])
    if (events.event_timestamp_ms > cutoff_ms).any():
        raise ValueError(f"events after the cutoff {cutoff}: features may not look past it")
    if not members.msno.is_unique or not labels.msno.is_unique:
        raise ValueError("members and labels must have one row per msno")

    own = events[events.type != "EXPIRATION"].copy()
    own["_order"] = own.type.map(_ORDER)
    own = own.sort_values(["app_user_id", "event_timestamp_ms", "_order"], kind="stable")
    by_user = own.groupby("app_user_id")
    last = by_user.tail(1).set_index("app_user_id")
    paid = own[own.type != "CANCELLATION"]
    signature = paid.plan_list_price.astype(str) + "/" + paid.payment_plan_days.astype(str)
    changed = signature != signature.groupby(paid.app_user_id).shift()
    changed &= signature.groupby(paid.app_user_id).cumcount() > 0

    counts = events.groupby(["app_user_id", "type"]).size().unstack(fill_value=0)
    counts = counts.reindex(columns=list(EVENT_TYPES), fill_value=0)
    counts.columns = [f"n_{name.lower()}" for name in EVENT_TYPES]
    users = counts.index

    out = counts.copy()
    out["n_transactions"] = by_user.size().reindex(users)
    out["plan_changes"] = changed.groupby(paid.app_user_id).sum().reindex(users).fillna(0)
    out["left_censored"] = by_user.left_censored.first().reindex(users).astype(int)
    out["ever_trial"] = (own.period_type == "TRIAL").groupby(own.app_user_id).any().reindex(users)
    out["ever_trial"] = out.ever_trial.astype(int)
    newest = events.groupby("app_user_id").event_timestamp_ms.max()
    out["days_since_last_event"] = (cutoff_ms - newest.reindex(users)) // _DAY_MS
    out["total_paid"] = paid.groupby("app_user_id").actual_amount_paid.sum().reindex(users)
    out["total_paid"] = out.total_paid.fillna(0)

    current = last.reindex(users)
    for column in ("payment_plan_days", "plan_list_price", "actual_amount_paid", "is_auto_renew"):
        out[f"last_{column}"] = current[column]
    out["last_payment_method_id"] = _level(current.payment_method_id)
    priced = current.plan_list_price > 0
    out["paid_vs_list"] = (current.actual_amount_paid / current.plan_list_price).where(priced)
    out["last_is_cancel"] = (current.type == "CANCELLATION").astype(int)
    usable = ~current.expiry_before_purchase.astype(bool)
    out["days_to_expiry"] = ((current.expiration_at_ms - cutoff_ms) // _DAY_MS).where(usable)

    member = members.set_index("msno").reindex(users)
    registered = to_date(member.registration_init_time.fillna(0))
    registered = registered.where(registered <= to_date(pd.Series([cutoff])).iloc[0])
    out["tenure_days"] = (
        to_date(pd.Series([cutoff] * len(users), index=users)) - registered
    ).dt.days
    out["city"] = _level(member.city)
    out["registered_via"] = _level(member.registered_via)
    out["gender"] = member.gender.fillna("unknown").astype(object)
    low, high = BD_RANGE
    age = member.bd.where((member.bd >= low) & (member.bd <= high))
    out["bd"] = age
    out["bd_missing"] = age.isna().astype(int)

    if raw_dates:
        out["registration_date_raw"] = member.registration_init_time.where(registered.notna())
        expiry = pd.to_datetime(current.expiration_at_ms.astype("float64"), unit="ms")
        out["expiry_date_raw"] = (
            expiry.dt.year * 10000 + expiry.dt.month * 100 + expiry.dt.day
        ).where(usable)

    target = labels.set_index("msno")[TARGET]
    out = out.loc[out.index.isin(target.index)]
    out[TARGET] = target.reindex(out.index).astype(int)
    out.index.name = "msno"
    return out.sort_index()
