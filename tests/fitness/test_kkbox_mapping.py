"""KKBox: candidate targets look only forward, features only back (Context, retrieval, memory).

`scripts/explore_kkbox.py` derives candidate targets from the raw transactions. The rule these
tests hold is the same one the feature builder will be held to: a target is computed from
transactions strictly **after** the cutoff and a feature-side summary from transactions at or
before it, and the two never share a row. A target that peeked at the feature window would
measure the label it was built from.

Runs with no data, no token and no network: the frames are built here.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType

import pandas as pd
import pytest

from agentstack.context import datasets, kkbox, targeting
from agentstack.prediction.churn import ChurnScores

ROOT = pathlib.Path(__file__).resolve().parents[2]
CUTOFF = 20170228


def _explore() -> ModuleType:
    """Load scripts/explore_kkbox.py, which is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "explore_kkbox", ROOT / "scripts" / "explore_kkbox.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


explore = _explore()


def tx(
    user: str,
    date: int,
    expires: int,
    *,
    price: int = 149,
    paid: int | None = None,
    days: int = 30,
    cancel: int = 0,
    auto: int = 1,
    method: int = 41,
) -> dict[str, object]:
    return {
        "msno": user,
        "payment_method_id": method,
        "payment_plan_days": days,
        "plan_list_price": price,
        "actual_amount_paid": price if paid is None else paid,
        "is_auto_renew": auto,
        "transaction_date": date,
        "membership_expire_date": expires,
        "is_cancel": cancel,
    }


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # a: renews on the same plan before it expires
            tx("a", 20170115, 20170310),
            tx("a", 20170305, 20170410),
            # b: renews onto a dearer plan, nine days late
            tx("b", 20170120, 20170305),
            tx("b", 20170314, 20170414, price=180, days=30),
            # c: renews onto a cheaper plan
            tx("c", 20170120, 20170305, price=180),
            tx("c", 20170301, 20170401, price=149),
            # d: never comes back
            tx("d", 20170120, 20170305),
            # e: turns auto-renew off with a cancel row, then renews 40 days after the
            # (shortened) expiry
            tx("e", 20170120, 20170315),
            tx("e", 20170210, 20170305, cancel=1, auto=0),
            tx("e", 20170415, 20170515),
        ]
    )


def last_of(history: pd.DataFrame) -> pd.DataFrame:
    last: pd.DataFrame = explore.last_pre_cutoff(history, CUTOFF, since=0)
    return last


def test_features_look_back_only() -> None:
    """A transaction after the cutoff never changes the feature-side row."""
    history = frame()
    before = last_of(history)
    later = pd.concat(
        [history, pd.DataFrame([tx("a", 20170320, 20170520, price=999)])], ignore_index=True
    )
    assert last_of(later).equals(before)
    assert (before.transaction_date <= CUTOFF).all()


def test_targets_look_forward_only() -> None:
    """A transaction before the cutoff never changes a candidate target."""
    history = frame()
    last = last_of(history)
    baseline = explore.derive_targets(history, last, CUTOFF)
    earlier = pd.concat(
        [history, pd.DataFrame([tx("a", 20170101, 20170201, price=999)])], ignore_index=True
    )
    assert explore.derive_targets(earlier, last, CUTOFF).equals(baseline)


def test_targets_move_when_a_later_transaction_does() -> None:
    """The converse, so the test above cannot pass by the targets ignoring everything."""
    history = frame()
    last = last_of(history)
    baseline = explore.derive_targets(history, last, CUTOFF)
    richer = pd.concat([history, pd.DataFrame([tx("d", 20170320, 20170420)])], ignore_index=True)
    changed = explore.derive_targets(richer, last, CUTOFF)
    assert not baseline.loc["d", "renewed_after"]
    assert changed.loc["d", "renewed_after"]
    assert changed.loc["d", "spend_after"] == 149


def test_candidate_targets_per_case() -> None:
    history = frame()
    targets = explore.derive_targets(history, last_of(history), CUTOFF)
    assert targets.loc["a", "plan_move"] == "same"
    assert targets.loc["b", "plan_move"] == "up"
    assert targets.loc["c", "plan_move"] == "down"
    assert targets.loc["d", "plan_move"] == "none"
    assert targets.loc["d", "spend_after"] == 0
    assert targets.loc["a", "spend_after"] == 149
    assert targets.loc["b", "first_paid_after"] == 180
    assert not targets["cancelled_after"].any()


def test_renewal_gap_follows_the_organisers() -> None:
    history = frame()
    last = last_of(history)
    gap = explore.renewal_gap(history, last, CUTOFF)
    assert gap["d"] == 9999
    assert gap["a"] <= 0
    assert gap["b"] == 9
    # e's last pre-cutoff row is the cancel, whose expiry is already the shortened one
    assert gap["e"] == 41


def test_a_cancel_shortens_the_expiry_the_gap_is_measured_from() -> None:
    history = pd.DataFrame(
        [
            tx("f", 20170120, 20170420),
            tx("f", 20170301, 20170310, cancel=1, auto=0),
            tx("f", 20170325, 20170425),
        ]
    )
    last = explore.last_pre_cutoff(history, 20170228, since=0)
    # after the cutoff: the cancel pulls the end from 04-20 back to 03-10, the renewal is 15 days on
    assert explore.renewal_gap(history, last, 20170228)["f"] == 15


def test_same_day_subscribe_precedes_cancel() -> None:
    history = pd.DataFrame(
        [
            tx("g", 20170210, 20170305, cancel=1, auto=0),
            tx("g", 20170210, 20170410),
        ]
    )
    ordered = explore.ordered(history)
    assert list(ordered.is_cancel) == [0, 1]


def test_explore_lives_outside_the_package() -> None:
    """Like fetch_datasets.py: it reads third-party files; nothing in `agentstack` imports it."""
    src = ROOT / "src" / "agentstack"
    assert (ROOT / "scripts" / "explore_kkbox.py").exists()
    assert not any("explore_kkbox" in p.read_text() for p in src.rglob("*.py"))


# ---------------------------------------------------------------------------------------------
# The RevenueCat mapping (agentstack.context.kkbox)

JAN_15_2017_MS = 1_484_438_400_000  # 2017-01-15T00:00:00Z


def events(history: pd.DataFrame, registration: dict[str, int] | None = None) -> pd.DataFrame:
    result: pd.DataFrame = kkbox.to_events(
        history, pd.Series(registration or {}, dtype="float64"), CUTOFF
    )
    return result


def types(frame: pd.DataFrame, user: str) -> list[str]:
    return list(frame[frame.app_user_id == user].type)


def test_first_transaction_soon_after_registration_is_an_initial_purchase() -> None:
    history = pd.DataFrame([tx("a", 20170115, 20170215), tx("a", 20170213, 20170315)])
    out = events(history, {"a": 20170110})
    assert types(out, "a") == ["INITIAL_PURCHASE", "RENEWAL"]
    assert not out.left_censored.any()
    assert out.iloc[0].purchased_at_ms == JAN_15_2017_MS


def test_a_first_transaction_long_after_registration_is_a_censored_renewal() -> None:
    history = pd.DataFrame([tx("a", 20170115, 20170315)])
    out = events(history, {"a": 20120101})
    assert types(out, "a") == ["RENEWAL"]
    assert out.left_censored.all()


def test_unknown_registration_is_a_censored_renewal_not_an_initial_purchase() -> None:
    out = events(pd.DataFrame([tx("a", 20170115, 20170315)]))
    assert types(out, "a") == ["RENEWAL"]
    assert out.left_censored.all()


def test_a_cancel_row_is_a_cancellation_with_its_own_expiry() -> None:
    history = pd.DataFrame(
        [tx("a", 20170101, 20170301), tx("a", 20170120, 20170210, cancel=1, auto=0)]
    )
    out = events(history, {"a": 20170101})
    cancel = out[out.type == "CANCELLATION"].iloc[0]
    assert cancel.expiration_at_ms == int(kkbox.to_ms(pd.Series([20170210])).iloc[0])
    assert not cancel.is_auto_renew


def test_expiry_is_derived_never_a_row_and_never_from_after_the_cutoff() -> None:
    history = pd.DataFrame(
        [
            tx("lapsed", 20170105, 20170205),  # expired 02-05, before the cutoff
            tx("alive", 20170105, 20170305),  # expires 03-05, after the cutoff
        ]
    )
    out = events(history)
    assert types(out, "lapsed") == ["RENEWAL", "EXPIRATION"]
    assert types(out, "alive") == ["RENEWAL"]


def test_a_renewal_after_the_expiry_is_a_lapse_then_a_resubscription() -> None:
    history = pd.DataFrame([tx("a", 20170105, 20170205), tx("a", 20170220, 20170320)])
    out = events(history)
    assert types(out, "a") == ["RENEWAL", "EXPIRATION", "RENEWAL"]
    expiration = out[out.type == "EXPIRATION"].iloc[0]
    assert expiration.expiration_at_ms == int(kkbox.to_ms(pd.Series([20170205])).iloc[0])


def test_a_renewal_before_the_expiry_is_not_a_lapse() -> None:
    history = pd.DataFrame([tx("a", 20170105, 20170205), tx("a", 20170201, 20170305)])
    assert "EXPIRATION" not in types(events(history), "a")


def test_nothing_after_the_cutoff_makes_an_event() -> None:
    history = frame()
    baseline = events(history)
    later = pd.concat(
        [history, pd.DataFrame([tx("a", 20170320, 20170420), tx("zz", 20170315, 20170415)])],
        ignore_index=True,
    )
    assert events(later).equals(baseline)
    assert (baseline.event_timestamp_ms <= kkbox.to_ms(pd.Series([CUTOFF])).iloc[0]).all()


def test_a_zero_payment_on_a_priced_plan_is_a_trial() -> None:
    history = pd.DataFrame([tx("a", 20170115, 20170215, paid=0), tx("b", 20170115, 20170215)])
    out = events(history)
    assert out[out.app_user_id == "a"].period_type.iloc[0] == "TRIAL"
    assert out[out.app_user_id == "b"].period_type.iloc[0] == "NORMAL"


def test_an_expiry_before_its_own_purchase_is_flagged_and_is_never_an_expiration() -> None:
    history = pd.DataFrame([tx("a", 20170115, 19700101)])
    out = events(history)
    assert out.expiry_before_purchase.all()
    assert "EXPIRATION" not in types(out, "a")


def test_fields_without_a_revenuecat_name_are_extras_not_schema() -> None:
    out = events(pd.DataFrame([tx("a", 20170115, 20170315, method=29)]))
    assert out.iloc[0].payment_method_id == 29
    assert "payment_method_id" not in kkbox.RC_COLUMNS
    assert set(kkbox.RC_COLUMNS) | set(kkbox.EXTRA_COLUMNS) == set(out.columns)
    assert set(out.type) <= set(kkbox.EVENT_TYPES)


def test_the_mapping_is_deterministic_and_row_order_independent() -> None:
    history = frame()
    shuffled = history.sample(frac=1, random_state=3).reset_index(drop=True)
    assert events(history).equals(events(shuffled))


# ---------------------------------------------------------------------------------------------
# One row per subscriber (agentstack.context.kkbox.to_features)


def member(
    user: str, *, registered: int = 20160101, bd: int = 30, gender: object = "male"
) -> dict[str, object]:
    return {
        "msno": user,
        "city": 1,
        "bd": bd,
        "gender": gender,
        "registered_via": 7,
        "registration_init_time": registered,
    }


def build(
    history: pd.DataFrame,
    members: list[dict[str, object]] | None = None,
    labels: dict[str, int] | None = None,
    *,
    raw_dates: bool = False,
    cutoff: int = CUTOFF,
) -> pd.DataFrame:
    people = members if members is not None else [member(u) for u in history.msno.unique()]
    churn = labels if labels is not None else {u: 0 for u in history.msno.unique()}
    registration = pd.Series(
        {m["msno"]: m["registration_init_time"] for m in people}, dtype="float64"
    )
    result: pd.DataFrame = kkbox.to_features(
        kkbox.to_events(history, registration, cutoff),
        pd.DataFrame(people, columns=list(member("x"))),
        pd.DataFrame({"msno": list(churn), "is_churn": list(churn.values())}),
        cutoff,
        raw_dates=raw_dates,
    )
    return result


def test_a_transaction_after_the_cutoff_changes_no_feature() -> None:
    history = frame()
    baseline = build(history)
    later = pd.concat(
        [history, pd.DataFrame([tx("a", 20170320, 20170420), tx("d", 20170301, 20170401)])],
        ignore_index=True,
    )
    assert build(later).equals(baseline)


def test_a_registration_after_the_cutoff_is_unknown_not_negative_tenure() -> None:
    history = pd.DataFrame([tx("a", 20170115, 20170315)])
    out = build(history, [member("a", registered=20170310)])
    assert pd.isna(out.loc["a", "tenure_days"])
    assert build(history, [member("a", registered=20170128)]).loc["a", "tenure_days"] == 31


def test_events_past_the_cutoff_are_refused_rather_than_ignored() -> None:
    history = pd.DataFrame([tx("a", 20170115, 20170315)])
    wide = kkbox.to_events(history, pd.Series(dtype="float64"), 20170331)
    with pytest.raises(ValueError, match="after the cutoff"):
        kkbox.to_features(
            wide, pd.DataFrame([member("a")]), pd.DataFrame({"msno": ["a"], "is_churn": [0]})
        )


def test_same_input_gives_the_same_frame_whatever_the_row_order() -> None:
    history = frame()
    shuffled = history.sample(frac=1, random_state=5).reset_index(drop=True)
    assert build(history).equals(build(shuffled))
    assert build(history).equals(build(history))


def test_the_label_is_joined_by_user_and_users_without_history_or_label_are_left_out() -> None:
    history = pd.DataFrame([tx("a", 20170115, 20170315), tx("b", 20170115, 20170315)])
    out = build(history, labels={"a": 1, "b": 0, "ghost": 1})
    assert list(out.index) == ["a", "b"]
    assert out.is_churn.to_dict() == {"a": 1, "b": 0}
    assert list(build(history, labels={"a": 1}).index) == ["a"]


def test_bd_outside_ten_to_ninety_is_null_with_a_missing_flag() -> None:
    history = pd.DataFrame([tx(u, 20170115, 20170315) for u in "abcde"])
    people = [
        member("a", bd=0),
        member("b", bd=9),
        member("c", bd=10),
        member("d", bd=90),
        member("e", bd=91),
    ]
    out = build(history, people)
    assert out.bd.isna().to_dict() == {"a": True, "b": True, "c": False, "d": False, "e": True}
    assert out.bd_missing.to_dict() == {"a": 1, "b": 1, "c": 0, "d": 0, "e": 1}


def test_a_user_with_no_members_row_gets_unknown_levels_not_a_dropped_row() -> None:
    out = build(pd.DataFrame([tx("a", 20170115, 20170315)]), members=[])
    row = out.loc["a"]
    assert (row.city, row.registered_via, row.gender) == ("unknown", "unknown", "unknown")
    assert row.bd_missing == 1 and pd.isna(row.tenure_days)


def test_codes_are_categories_and_are_never_numbers() -> None:
    out = build(pd.DataFrame([tx("a", 20170115, 20170315, method=29)]))
    for column in ("city", "registered_via", "last_payment_method_id", "gender"):
        assert not pd.api.types.is_numeric_dtype(out[column])
    assert out.loc["a", "last_payment_method_id"] == "29"


def test_each_feature_group_reads_the_events() -> None:
    history = pd.DataFrame(
        [
            tx("a", 20170105, 20170205, price=149),
            tx("a", 20170205, 20170305, price=180, paid=0),
            tx("a", 20170210, 20170305, price=180, paid=0, cancel=1, auto=0),
        ]
    )
    row = build(history).loc["a"]
    assert row.n_transactions == 3
    assert row.n_cancellation == 1 and row.last_is_cancel == 1
    assert row.plan_changes == 1 and row.ever_trial == 1
    assert row.left_censored == 1  # registered 2016-01-01, first transaction a year later
    assert row.total_paid == 149  # the cancel row is not a second payment
    assert row.days_to_expiry == 5 and row.days_since_last_event == 18
    assert (
        row.last_is_auto_renew == 0 if "last_is_auto_renew" in row else row.last_is_auto_renew == 0
    )


def test_a_lapse_shows_as_an_expiration_count_and_days_since_the_last_event() -> None:
    row = build(pd.DataFrame([tx("a", 20170105, 20170205)])).loc["a"]
    assert row.n_expiration == 1 and row.days_to_expiry == -23
    assert row.days_since_last_event == 23


def test_raw_dates_are_candidates_off_by_default() -> None:
    history = pd.DataFrame([tx("a", 20170105, 20170305)])
    assert not set(kkbox.CANDIDATE_COLUMNS) & set(build(history).columns)
    row = build(history, raw_dates=True).loc["a"]
    assert (row.registration_date_raw, row.expiry_date_raw) == (20160101, 20170305)


def test_identifiers_and_raw_dates_are_excluded_with_a_reason() -> None:
    out = build(pd.DataFrame([tx("a", 20170105, 20170305)]), raw_dates=True)
    assert out.index.name == "msno"
    assert not set(kkbox.EXCLUDED) & set(out.columns)
    assert all(reason.strip() for reason in kkbox.EXCLUDED.values())


def test_two_rows_for_one_user_in_members_or_labels_are_refused() -> None:
    history = pd.DataFrame([tx("a", 20170115, 20170315)])
    out = kkbox.to_events(history, pd.Series(dtype="float64"), CUTOFF)
    one = pd.DataFrame({"msno": ["a"], "is_churn": [0]})
    with pytest.raises(ValueError, match="one row per msno"):
        kkbox.to_features(out, pd.DataFrame([member("a"), member("a")]), one)
    with pytest.raises(ValueError, match="one row per msno"):
        kkbox.to_features(out, pd.DataFrame([member("a")]), pd.concat([one, one]))


# The registered cohort (agentstack.context.datasets.REGISTRY["kkbox-churn"])


def cohort_file(users: int = 20) -> pd.DataFrame:
    """What `scripts/build_kkbox_cohort.py` writes: `to_features` output, `msno` as a column."""
    rows = [tx(f"u{i:02d}", 20170115, 20170315, paid=100 + i) for i in range(users)]
    labels = {f"u{i:02d}": int(i % 4 == 0) for i in range(users)}
    return build(pd.DataFrame(rows), labels=labels).reset_index()


def test_kkbox_is_registered_in_its_own_currency_with_observed_revenue() -> None:
    spec = datasets.REGISTRY["kkbox-churn"]

    assert spec.target == kkbox.TARGET
    assert spec.currency == "NTD"
    assert spec.revenue_columns == ("last_actual_amount_paid",)
    assert spec.revenue_period_days_column == "last_payment_plan_days"
    assert spec.revenue_unpaid_flag_column == "last_is_cancel"
    assert "plan days" in spec.revenue_note and "annualised" in spec.revenue_note


def test_the_cohort_file_loads_with_msno_dropped_for_its_stated_reason() -> None:
    spec = datasets.REGISTRY["kkbox-churn"]
    snapshot = datasets.snapshot(spec, cohort_file())

    assert "msno" not in snapshot.columns
    assert spec.drops["msno"] == kkbox.EXCLUDED["msno"]
    assert snapshot.churn_rate == pytest.approx(0.25)
    assert set(snapshot.columns) >= {"last_actual_amount_paid", "tenure_days", "is_churn"}


def test_the_same_cohort_file_has_the_same_data_as_of_and_a_changed_cell_moves_it() -> None:
    spec = datasets.REGISTRY["kkbox-churn"]
    moved = cohort_file()
    moved.loc[0, "last_actual_amount_paid"] += 1

    assert (
        datasets.snapshot(spec, cohort_file()).data_as_of
        == datasets.snapshot(spec, cohort_file()).data_as_of
    )
    assert (
        datasets.snapshot(spec, moved).data_as_of
        != datasets.snapshot(spec, cohort_file()).data_as_of
    )


def test_kkbox_is_targetable_and_its_value_at_risk_is_in_ntd() -> None:
    snapshot = datasets.snapshot(datasets.REGISTRY["kkbox-churn"], cohort_file())
    scored = ChurnScores(
        dataset="kkbox-churn",
        data_as_of=snapshot.data_as_of,
        model_version="tabpfn-3.5",
        folds=5,
        seed=7,
        probabilities=tuple(i / snapshot.rows for i in range(snapshot.rows)),
        positives=5,
    )
    rule = targeting.TargetingRule(
        profile="test",
        risk_quantile=0.9,
        minimum_cohort=2,
        minimum_annual_value_at_risk_cents=1_000,
    )
    cohort = targeting.select(snapshot, scored, rule=rule)

    # the riskiest decile of 20 is the last two users, paying 118 and 119 for a 30-day plan
    assert cohort.annual_value_at_risk_cents == (118 + 119) * 100 * 365 // 30
    assert cohort.currency == "NTD"
    assert "NTD" in cohort.description()["currency"]


def revenue_of(rows: list[dict[str, object]]) -> list[int]:
    snapshot = datasets.snapshot(
        datasets.REGISTRY["kkbox-churn"], build(pd.DataFrame(rows)).reset_index()
    )
    return [int(v) for v in targeting.annual_revenue_cents(snapshot)]


def test_annual_revenue_follows_the_length_of_the_plan_that_was_paid_for() -> None:
    thirty = tx("a", 20170115, 20170215, paid=149, days=30)
    long_plan = tx("b", 20170115, 20180225, paid=1788, days=410)
    seven = tx("c", 20170115, 20170122, paid=35, days=7)

    month, plan410, week = revenue_of([thirty, long_plan, seven])

    assert month == round(149 * 365 / 30 * 100)
    # one payment for 410 days is about 1,592 a year, not 1,788 x 12
    assert plan410 == round(1788 * 365 / 410 * 100)
    assert plan410 < 1788 * 100 * 1.01
    assert week == round(35 * 365 / 7 * 100)


def test_a_last_event_that_is_a_cancellation_is_not_an_observed_payment() -> None:
    paid = tx("a", 20170115, 20170215, paid=149)
    cancelled = [tx("b", 20170115, 20170215, paid=149), tx("b", 20170201, 20170201, cancel=1)]

    assert revenue_of([paid])[0] > 0
    assert revenue_of([*cancelled])[0] == 0


def test_a_plan_with_no_length_is_worth_nothing_not_a_guess() -> None:
    assert revenue_of([tx("a", 20170115, 20170215, paid=149, days=0)]) == [0]


def test_bank_churn_is_still_loadable_and_still_not_targetable() -> None:
    assert datasets.REGISTRY["bank-churn"].revenue_columns == ()


def test_an_absent_derived_cohort_names_its_build_script_not_the_downloader(
    tmp_path: pathlib.Path,
) -> None:
    spec = datasets.REGISTRY["kkbox-churn"]

    assert spec.derived_by == "scripts/build_kkbox_cohort.py"
    with pytest.raises(datasets.DatasetError, match="build_kkbox_cohort"):
        datasets.load("kkbox-churn", root=tmp_path)
    assert datasets.REGISTRY["telecom-bigml"].derived_by == ""


# ---------------------------------------------------------------------------------------------
# The declared cut (agentstack.context.kkbox.sample_users)


def population(users: int = 1000, churn_every: int = 10) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "msno": [f"u{i:05d}" for i in range(users)],
            "is_churn": [int(i % churn_every == 0) for i in range(users)],
        }
    )


def test_the_cut_is_deterministic_and_does_not_depend_on_row_order() -> None:
    labels = population()
    first = kkbox.sample_users(labels, 200, seed=7)

    assert first.equals(kkbox.sample_users(labels, 200, seed=7))
    assert first.equals(kkbox.sample_users(labels.sample(frac=1, random_state=3), 200, seed=7))
    assert not first.equals(kkbox.sample_users(labels, 200, seed=8))


def test_the_cut_keeps_the_label_balance() -> None:
    labels = population()
    cut = kkbox.sample_users(labels, 200, seed=7)
    rate = labels.set_index("msno").loc[cut, "is_churn"].mean()

    assert len(cut) == 200
    assert rate == pytest.approx(labels.is_churn.mean(), abs=0.005)
    assert cut.is_unique


def test_a_cut_at_or_above_the_population_is_the_whole_population() -> None:
    labels = population(50)

    assert list(kkbox.sample_users(labels, 50, seed=1)) == sorted(labels.msno)
    assert list(kkbox.sample_users(labels, 500, seed=1)) == sorted(labels.msno)


def test_a_rare_class_is_never_sampled_out_of_existence() -> None:
    labels = population(1000, churn_every=500)
    cut = kkbox.sample_users(labels, 10, seed=1)

    assert labels.set_index("msno").loc[cut, "is_churn"].sum() >= 1


def test_the_cut_refuses_what_it_cannot_stratify() -> None:
    with pytest.raises(ValueError, match="one row per msno"):
        kkbox.sample_users(pd.concat([population(10), population(10)]), 5, seed=1)
    with pytest.raises(ValueError, match="positive"):
        kkbox.sample_users(population(10), 0, seed=1)


def test_the_cut_is_a_declared_size_and_the_build_script_stays_outside_the_package() -> None:
    assert kkbox.COHORT_USERS > 0
    assert (ROOT / "scripts" / "build_kkbox_cohort.py").exists()
    src = ROOT / "src" / "agentstack"
    assert not any("import build_kkbox_cohort" in p.read_text() for p in src.rglob("*.py"))


# K13: a raw-date candidate is admitted only if it passes the three rules


def candidates(users: int = 200) -> pd.DataFrame:
    """A cohort-shaped frame with both candidate columns, dates shared by many users."""
    return pd.DataFrame(
        {
            "registration_date_raw": [20160101 + (i % 20) for i in range(users)],
            "expiry_date_raw": [20170301 + (i % 28) for i in range(users)],
            "is_churn": [(i * 7) % 11 == 0 for i in range(users)],
        }
    ).astype({"is_churn": int})


def test_a_candidate_that_passes_all_three_rules_is_admitted() -> None:
    assert kkbox.candidate_refusals(candidates(), CUTOFF) == {}


def test_a_registration_after_the_cutoff_is_refused_as_unknown_at_the_cutoff() -> None:
    frame = candidates()
    frame.loc[3, "registration_date_raw"] = CUTOFF + 1

    refused = kkbox.candidate_refusals(frame, CUTOFF)

    assert set(refused) == {"registration_date_raw"}
    assert "after the cutoff" in refused["registration_date_raw"]


def test_a_candidate_that_names_each_row_is_refused_as_an_identifier() -> None:
    frame = candidates()
    frame["expiry_date_raw"] = range(20170301, 20170301 + len(frame))

    assert set(kkbox.candidate_refusals(frame, CUTOFF)) == {"expiry_date_raw"}
    assert "identifier" in kkbox.candidate_refusals(frame, CUTOFF)["expiry_date_raw"]


def test_a_candidate_that_correlates_with_the_label_like_an_outcome_is_refused() -> None:
    frame = candidates()
    frame["registration_date_raw"] = 20160101 + frame.is_churn

    refused = kkbox.candidate_refusals(frame, CUTOFF)

    assert set(refused) == {"registration_date_raw"}
    assert "0.9" in refused["registration_date_raw"]


def test_the_rules_ignore_a_candidate_that_is_not_in_the_frame() -> None:
    assert kkbox.candidate_refusals(candidates().drop(columns=["expiry_date_raw"]), CUTOFF) == {}
