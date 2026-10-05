"""Criterion 17: targeting is real and reproducible.

The same snapshot and the same model version produce the same eligible cohort, and the
cohort's size and risk distribution are recorded on the experiment. A cohort that
cannot be reproduced cannot be rolled out to.

Criterion 11 rides along: every control subject must pass the same predicate at the
same model version as every treatment subject, so membership has to be re-checkable
after the fact rather than implied by whoever built the list.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from agentstack.context import targeting
from agentstack.context.datasets import REGISTRY, CohortSnapshot, DatasetSpec
from agentstack.context.frozen_cohorts import FrozenCohort
from agentstack.context.targeting import (
    CohortTooSmall,
    NotEnoughAtRisk,
    RevenueNotObserved,
    TargetingRefused,
    TargetingRule,
)
from agentstack.prediction.churn import ChurnScores

ROWS = 400
DEV = TargetingRule(
    profile="test",
    risk_quantile=0.9,
    minimum_cohort=10,
    minimum_annual_value_at_risk_cents=1_000,
    floors_cents=(("NTD", 1_000),),
)


@pytest.fixture(autouse=True)
def _registered() -> object:
    """A dataset with observed revenue, registered the way a real one is."""
    REGISTRY["fixture"] = DatasetSpec(
        key="fixture",
        kaggle="nobody/nothing",
        files=("fixture.csv",),
        target="Churn",
        churned="1",
        drops={},
        revenue_columns=("monthly charge",),
        revenue_periods_per_year=12,
        revenue_note="fixture revenue, annualised x12",
    )
    yield None
    REGISTRY.pop("fixture", None)


def snapshot(rows: int = ROWS, charge: float = 50.0) -> CohortSnapshot:
    frame = pd.DataFrame(
        {
            "monthly charge": [charge] * rows,
            "Churn": [i % 4 == 0 for i in range(rows)],
        }
    ).astype({"Churn": int})
    return CohortSnapshot(
        dataset="fixture",
        data_as_of="fixture:abc123",
        rows=rows,
        columns=tuple(frame.columns),
        churn_rate=float(frame["Churn"].mean()),
        frame=frame,
    )


def scores(rows: int = ROWS, **overrides: object) -> ChurnScores:
    base = ChurnScores(
        dataset="fixture",
        data_as_of="fixture:abc123",
        model_version="tabpfn-3.5",
        folds=5,
        seed=7,
        # Strictly increasing, so the top decile is the last tenth and is knowable.
        probabilities=tuple(i / rows for i in range(rows)),
        positives=rows // 4,
    )
    return replace(base, **overrides)


# --- criterion 17 ---


def test_the_same_snapshot_and_model_produce_the_same_cohort() -> None:
    first = targeting.select(snapshot(), scores(), rule=DEV)
    second = targeting.select(snapshot(), scores(), rule=DEV)

    assert first.members == second.members
    assert first.experiment_version == second.experiment_version
    assert first.risk_threshold == second.risk_threshold


def test_a_different_model_version_is_a_different_experiment() -> None:
    """If the model changes the eligible population changes, and pretending otherwise
    is how an approval granted for one cohort authorises a rollout to another."""
    a = targeting.select(snapshot(), scores(), rule=DEV)
    b = targeting.select(snapshot(), scores(model_version="tabpfn-4"), rule=DEV)

    assert a.experiment_version != b.experiment_version


def test_a_moved_snapshot_is_a_different_experiment() -> None:
    moved = replace(snapshot(), data_as_of="fixture:moved")
    a = targeting.select(snapshot(), scores(), rule=DEV)
    b = targeting.select(moved, scores(data_as_of="fixture:moved"), rule=DEV)

    assert a.experiment_version != b.experiment_version


def test_a_changed_threshold_is_a_different_experiment() -> None:
    a = targeting.select(snapshot(), scores(), rule=DEV)
    b = targeting.select(snapshot(), scores(), rule=replace(DEV, risk_quantile=0.8))

    assert a.experiment_version != b.experiment_version
    assert b.size > a.size


def test_the_profile_name_is_part_of_the_version() -> None:
    """A dev cohort must not be mistakable for a production one."""
    a = targeting.select(snapshot(), scores(), rule=DEV)
    b = targeting.select(snapshot(), scores(), rule=replace(DEV, profile="production"))

    assert a.experiment_version != b.experiment_version
    assert a.description()["targeting_profile"] == "test"


def test_scores_from_another_snapshot_are_refused() -> None:
    with pytest.raises(TargetingRefused, match="not this population"):
        targeting.select(snapshot(), scores(data_as_of="fixture:elsewhere"), rule=DEV)


def test_a_score_count_mismatch_is_refused() -> None:
    with pytest.raises(TargetingRefused, match="scores for"):
        targeting.select(snapshot(rows=400), scores(rows=399), rule=DEV)


# --- the cut itself ---


def test_the_top_decile_is_the_top_decile() -> None:
    cohort = targeting.select(snapshot(), scores(), rule=DEV)

    assert cohort.size == 40
    assert cohort.members == tuple(range(360, 400))


def test_ties_do_not_widen_the_cohort() -> None:
    """A quantile threshold with ties returns more than a decile. The cohort size is
    what the minimum-size gate is about and what the approver is shown, so the cut is
    on rank, and ties break on row order rather than on whatever sort was stable."""
    tied = scores(probabilities=tuple(0.5 for _ in range(ROWS)))

    cohort = targeting.select(snapshot(), tied, rule=DEV)

    assert cohort.size == 40
    assert cohort.members == tuple(range(40))


def test_the_threshold_is_the_lowest_score_admitted() -> None:
    cohort = targeting.select(snapshot(), scores(), rule=DEV)

    assert cohort.risk_threshold == pytest.approx(360 / ROWS)


# --- the gates refuse rather than adjust ---


def test_a_cohort_under_the_minimum_is_refused_not_widened() -> None:
    """Widening the cut to qualify is how the top decile becomes the top third."""
    with pytest.raises(CohortTooSmall, match="would make the targeting mean something else"):
        targeting.select(snapshot(), scores(), rule=replace(DEV, minimum_cohort=100))


def test_a_cohort_worth_too_little_is_refused() -> None:
    with pytest.raises(NotEnoughAtRisk, match=r"\$50,000"):
        targeting.select(
            snapshot(),
            scores(),
            rule=replace(DEV, minimum_annual_value_at_risk_cents=5_000_000),
        )


def test_a_refusal_names_the_cohorts_own_currency() -> None:
    """A KKBox cohort is billed in NTD. A refusal that says `$50,000` about NTD is a
    wrong number said confidently."""
    REGISTRY["fixture"] = replace(REGISTRY["fixture"], currency="NTD")

    with pytest.raises(NotEnoughAtRisk, match=r"NTD 50,000") as refused:
        targeting.select(
            snapshot(),
            scores(),
            rule=replace(DEV, floors_cents=(("NTD", 5_000_000),)),
        )
    assert "$" not in str(refused.value)


def test_a_cohort_is_held_to_the_floor_stated_in_its_own_currency() -> None:
    """The audit's Important 2: one number in cents was compared against every cohort, so
    NTD 1.5M (about $50,000) was judged against a floor written as $50,000 - a gate about
    thirty times too weak. Here the USD floor would pass and the NTD floor must refuse."""
    REGISTRY["fixture"] = replace(REGISTRY["fixture"], currency="NTD")
    usd_floor_would_pass = replace(DEV, minimum_annual_value_at_risk_cents=1_000)

    with pytest.raises(NotEnoughAtRisk, match="NTD"):
        targeting.select(
            snapshot(),
            scores(),
            rule=replace(usd_floor_would_pass, floors_cents=(("NTD", 10_000_000_000),)),
        )


def test_a_currency_with_no_stated_floor_is_refused_not_judged_by_another() -> None:
    REGISTRY["fixture"] = replace(REGISTRY["fixture"], currency="NTD")

    with pytest.raises(targeting.NoFloorForCurrency, match="NTD"):
        targeting.select(snapshot(), scores(), rule=replace(DEV, floors_cents=()))


def test_every_profile_states_a_floor_for_every_currency_a_cohort_is_billed_in() -> None:
    """A dataset registered in a new currency must not reach a rule that cannot judge it."""
    currencies = {spec.currency for key, spec in REGISTRY.items() if key != "fixture"}
    for profile in ("default", "dev"):
        rule = TargetingRule.load(profile)
        for currency in currencies:
            assert rule.floor_cents(currency) > 0, f"{profile} has no floor for {currency}"


def test_the_stated_ntd_floor_is_the_dollar_floor_at_the_exchange_rate_it_names() -> None:
    """experiments/targeting.toml says $50,000 is NTD 1,500,000 at about 30. If either
    moves on its own, the two floors no longer describe the same bar."""
    rule = TargetingRule.load("default")

    assert rule.floor_cents("NTD") == 30 * rule.floor_cents("USD")


def test_usd_is_the_default_and_leaves_the_cohort_byte_identical() -> None:
    """`experiment_version` names a population in recorded histories and in live runs.
    The literal below was computed before `currency` existed; if it moves, every USD
    cohort already frozen has been renamed."""
    cohort = targeting.select(snapshot(), scores(), rule=DEV)

    assert REGISTRY["fixture"].currency == "USD"
    assert cohort.experiment_version == "exp:9c93736ad3fe0a25"
    assert "currency" not in cohort.description()
    assert cohort.currency == "USD"


def test_a_non_usd_cohort_records_its_currency() -> None:
    REGISTRY["fixture"] = replace(REGISTRY["fixture"], currency="NTD")

    cohort = targeting.select(snapshot(), scores(), rule=DEV)

    assert cohort.currency == "NTD"
    assert cohort.description()["currency"] == "NTD"
    # Its floor is a different number, so it is a different rule and a different version.
    # A USD cohort's name must not move (above).
    assert cohort.experiment_version != "exp:9c93736ad3fe0a25"
    assert cohort.description()["rule"].endswith(":NTD")


def test_a_frozen_cohort_reads_its_currency_from_what_was_recorded() -> None:
    """Rows frozen before `currency` existed have no such key; they were all USD."""
    usd = targeting.select(snapshot(), scores(), rule=DEV)
    REGISTRY["fixture"] = replace(REGISTRY["fixture"], currency="NTD")
    ntd = targeting.select(snapshot(), scores(), rule=DEV)

    def frozen(cohort: targeting.Cohort) -> FrozenCohort:
        return FrozenCohort(
            tenant="t",
            experiment_id="e",
            experiment_version=cohort.experiment_version,
            data_as_of=cohort.data_as_of,
            targeting_model_version=cohort.model_version,
            risk_threshold=cohort.risk_threshold,
            size=cohort.size,
            annual_value_at_risk_cents=cohort.annual_value_at_risk_cents,
            description=cohort.description(),
        )

    assert frozen(usd).currency == "USD"
    assert frozen(ntd).currency == "NTD"


def test_the_draft_turn_is_told_what_the_churn_model_found() -> None:
    """The drafting model writes why an offer should retain these customers. It is given
    the frozen cohort's TabPFN profile to reason from, not just the cohort's name."""
    from datetime import UTC, datetime

    from agentstack.policy.triggers import Outcome, TriggerKind
    from agentstack.runtime.cycles import Cycle
    from agentstack.runtime.drafting import draft_instruction

    cohort = targeting.select(snapshot(charge=10.0), scores(), rule=DEV)
    frozen = FrozenCohort(
        tenant="t",
        experiment_id="e",
        experiment_version=cohort.experiment_version,
        data_as_of=cohort.data_as_of,
        targeting_model_version=cohort.model_version,
        risk_threshold=cohort.risk_threshold,
        size=cohort.size,
        annual_value_at_risk_cents=cohort.annual_value_at_risk_cents,
        description=cohort.description(),
    )
    cycle = Cycle(
        experiment_id="e",
        data_as_of=cohort.data_as_of,
        kind=TriggerKind.DATA_ARRIVAL,
        outcome=Outcome.PROPOSE,
        run_id=cohort.experiment_version,
        claimed_at=datetime.now(UTC),
        settled_at=None,
    )

    told = draft_instruction(tenant="t", cycle=cycle, cohort=frozen)

    assert f"Churn model {cohort.model_version}" in told
    assert f"{cohort.size} customers" in told
    assert f"{cohort.risk_threshold:.2f} or above" in told
    assert targeting.money(cohort.annual_value_at_risk_cents) in told


def test_value_at_risk_uses_observed_revenue_annualised() -> None:
    cohort = targeting.select(snapshot(charge=10.0), scores(), rule=DEV)

    assert cohort.annual_value_at_risk_cents == 40 * 10_00 * 12


def test_a_dataset_without_observed_revenue_cannot_be_targeted() -> None:
    """The floor is specified against observed ARPU. Turning a deposit balance into
    revenue needs a margin assumption, and that would make the dollar figure a
    prediction - the one thing it is not allowed to be."""
    REGISTRY["fixture"] = replace(
        REGISTRY["fixture"], revenue_columns=(), revenue_periods_per_year=0
    )

    with pytest.raises(RevenueNotObserved, match="cannot be targeted"):
        targeting.select(snapshot(), scores(), rule=DEV)


def test_a_missing_revenue_column_is_refused_rather_than_treated_as_zero() -> None:
    REGISTRY["fixture"] = replace(REGISTRY["fixture"], revenue_columns=("not here",))

    with pytest.raises(RevenueNotObserved, match="not present"):
        targeting.select(snapshot(), scores(), rule=DEV)


# --- criterion 11: membership is re-checkable ---


def test_membership_can_be_rechecked_after_the_fact() -> None:
    """Every control subject must pass the same predicate as every treatment subject.
    A control drawn from the general base regresses differently from the targeted arm
    and manufactures a saving that was never there."""
    cohort = targeting.select(snapshot(), scores(), rule=DEV)

    assert cohort.includes(399)
    assert not cohort.includes(0)
    assert all(cohort.includes(m) for m in cohort.members)


def test_the_description_records_what_an_approver_needs() -> None:
    cohort = targeting.select(snapshot(), scores(), rule=DEV)

    described = cohort.description()

    assert described["size"] == 40
    assert described["targeting_model_version"] == "tabpfn-3.5"
    assert described["data_as_of"] == "fixture:abc123"
    assert described["annual_value_at_risk_cents"] == 40 * 50_00 * 12
    assert set(described["risk_quantiles"]) == {"0.0", "0.25", "0.5", "0.75", "1.0"}
    assert "annualised" in str(described["revenue_basis"])


def test_the_risk_distribution_is_recorded_not_just_the_size() -> None:
    """Two cohorts of 40 can be very different populations."""
    cohort = targeting.select(snapshot(), scores(), rule=DEV)

    quantiles = cohort.risk_quantiles

    assert list(quantiles) == sorted(quantiles)
    assert quantiles[0] == pytest.approx(cohort.risk_threshold)


# --- the rule file ---


def test_the_shipped_profiles_load() -> None:
    for profile in ("default", "dev"):
        rule = TargetingRule.load(profile)
        assert rule.profile == profile
        assert 0.0 < rule.risk_quantile < 1.0


def test_the_default_profile_is_the_specified_one() -> None:
    """SPEC.md: top decile, 1,000 customers, $50,000 annualised."""
    rule = TargetingRule.load("default")

    assert rule.risk_quantile == 0.9
    assert rule.minimum_cohort == 1000
    assert rule.minimum_annual_value_at_risk_cents == 5_000_000


def test_the_dev_profile_says_what_it_relaxes_and_why() -> None:
    """A relaxed threshold with no recorded reason is indistinguishable from a bar
    that was lowered to make something pass."""
    text = Path(targeting.DEFAULTS).read_text()

    assert "3,333" in text
    assert "not enough to measure" in text
    assert TargetingRule.load("dev").minimum_cohort < TargetingRule.load("default").minimum_cohort


def test_an_unknown_profile_is_refused() -> None:
    with pytest.raises(TargetingRefused, match="not a targeting profile"):
        TargetingRule.load("whatever-makes-this-pass")


def test_a_rule_that_selects_everyone_is_refused() -> None:
    with pytest.raises(TargetingRefused, match="selects everyone or no one"):
        replace(DEV, risk_quantile=0.0)


def test_a_rule_with_an_empty_cohort_minimum_is_refused() -> None:
    with pytest.raises(TargetingRefused, match="a cohort of zero is not an experiment"):
        replace(DEV, minimum_cohort=0)
