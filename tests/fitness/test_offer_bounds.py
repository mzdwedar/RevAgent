"""Bounded discretion (ADR-0015): the bounds are policy, loaded from config, never context.

The model chooses an offer and a percentage. What it may choose is held here, in layer 8,
and a check that fails says why in words admission can quote to a person.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agentstack.policy.offers import DEFAULTS, Bounds, OfferRefused, describe_offer, offer_cost

ANNUAL_ARPU = 12_000  # 120.00 a year, 10.00 a month, in the cohort's currency


def test_the_signed_off_bounds_are_what_the_config_says() -> None:
    bounds = Bounds.load("default")

    assert (bounds.percentage_min, bounds.percentage_max) == (5, 15)
    assert bounds.minimum_treatment_arm == 100
    assert bounds.cost_cap_bps == 500  # 5% of annual value at risk
    assert set(bounds.kinds) == {"percent_off", "free_months", "tier_upgrade"}
    assert (bounds.kinds["percent_off"].amount_min, bounds.kinds["percent_off"].amount_max) == (
        10,
        50,
    )
    assert (bounds.kinds["percent_off"].months_min, bounds.kinds["percent_off"].months_max) == (
        1,
        3,
    )
    assert (bounds.kinds["free_months"].amount_min, bounds.kinds["free_months"].amount_max) == (
        1,
        2,
    )
    assert (bounds.kinds["tier_upgrade"].months_min, bounds.kinds["tier_upgrade"].months_max) == (
        1,
        3,
    )


def test_bounds_come_only_from_config_never_from_context(tmp_path: Path) -> None:
    # The loader's whole input is a profile name and a path. There is no parameter through
    # which retrieved text, memory or a prompt could move a bound.
    assert list(inspect.signature(Bounds.load).parameters) == ["profile", "path"]

    toml = tmp_path / "offers.toml"
    toml.write_text(
        "[profiles.p]\n"
        "percentage_min = 7\npercentage_max = 9\nminimum_treatment_arm = 20\n"
        "cost_cap_bps = 100\n"
        "[profiles.p.kinds.free_months]\n"
        "amount_min = 1\namount_max = 1\nmonths_min = 1\nmonths_max = 1\n"
        "cost_basis = 'months_of_arpu'\n"
    )
    bounds = Bounds.load("p", toml)

    assert (bounds.percentage_min, bounds.percentage_max, bounds.cost_cap_bps) == (7, 9, 100)
    assert set(bounds.kinds) == {"free_months"}
    field = "percentage_max"
    with pytest.raises(FrozenInstanceError):
        setattr(bounds, field, 99)


def test_a_profile_that_is_not_in_the_config_is_refused() -> None:
    with pytest.raises(OfferRefused, match="not an offers profile"):
        Bounds.load("nope", DEFAULTS)


def test_a_missing_bound_is_refused_not_defaulted(tmp_path: Path) -> None:
    toml = tmp_path / "offers.toml"
    toml.write_text("[profiles.p]\npercentage_min = 5\n")
    with pytest.raises(OfferRefused, match="percentage_max"):
        Bounds.load("p", toml)


def test_an_unknown_kind_is_refused_with_a_reason_and_has_no_cost() -> None:
    bounds = Bounds.load("default")

    reason = bounds.check_offer("gift_card", 10, 1)
    assert reason is not None
    assert "gift_card" in reason
    assert "percent_off" in reason  # names what is allowed
    with pytest.raises(OfferRefused):
        offer_cost(bounds, "gift_card", 10, 1, ANNUAL_ARPU, "USD")


@pytest.mark.parametrize(
    ("kind", "amount", "months", "ok"),
    [
        ("percent_off", 10, 1, True),
        ("percent_off", 50, 3, True),
        ("percent_off", 9, 1, False),
        ("percent_off", 51, 1, False),
        ("percent_off", 20, 0, False),
        ("percent_off", 20, 4, False),
        ("free_months", 1, 1, True),
        ("free_months", 2, 1, True),
        ("free_months", 3, 1, False),
        ("free_months", 0, 1, False),
        ("tier_upgrade", 1, 1, True),
        ("tier_upgrade", 1, 3, True),
        ("tier_upgrade", 1, 4, False),
    ],
)
def test_each_offer_bound_is_inclusive_and_quotes_itself(
    kind: str, amount: int, months: int, ok: bool
) -> None:
    reason = Bounds.load("default").check_offer(kind, amount, months)

    if ok:
        assert reason is None
    else:
        assert isinstance(reason, str)
        assert kind in reason


@pytest.mark.parametrize(("pct", "ok"), [(4, False), (5, True), (15, True), (16, False)])
def test_the_percentage_range_is_inclusive(pct: int, ok: bool) -> None:
    # 5% of 10,000 is 500 customers: the arm floor is not what is being tested here.
    reason = Bounds.load("default").check_percentage(pct, cohort_size=10_000)

    assert (reason is None) is ok
    if not ok:
        assert "5" in str(reason)
        assert "15" in str(reason)


def test_the_treatment_arm_must_be_at_least_the_floor() -> None:
    bounds = Bounds.load("default")

    # On 1,000 customers 5% is 50: under the 100-customer floor, so the range is narrower
    # than it looks. 10% is exactly 100.
    reason = bounds.check_percentage(5, cohort_size=1_000)
    assert reason is not None
    assert "100" in reason
    assert "50" in reason  # the arm it would have been
    assert bounds.check_percentage(9, cohort_size=1_000) is not None
    assert bounds.check_percentage(10, cohort_size=1_000) is None


def test_cost_is_computed_from_observed_arpu_in_integer_cents() -> None:
    bounds = Bounds.load("default")

    # 10.00 a month: 20% off for 3 months is 6.00; one free month is 10.00.
    assert offer_cost(bounds, "percent_off", 20, 3, ANNUAL_ARPU, "USD") == 600
    assert offer_cost(bounds, "free_months", 2, 1, ANNUAL_ARPU, "USD") == 2_000
    upgrade = offer_cost(bounds, "tier_upgrade", 1, 2, ANNUAL_ARPU, "USD")
    assert upgrade == offer_cost(bounds, "tier_upgrade", 1, 2, ANNUAL_ARPU, "USD")
    assert upgrade > 0


def test_cost_is_deterministic_scales_with_arpu_and_rounds_up() -> None:
    bounds = Bounds.load("default")

    first = offer_cost(bounds, "percent_off", 33, 1, 12_345, "NTD")
    assert first == offer_cost(bounds, "percent_off", 33, 1, 12_345, "NTD")
    assert offer_cost(bounds, "percent_off", 33, 1, 24_690, "NTD") >= 2 * first - 1
    # 12_345 * 33 / 1200 = 339.5...: a cost is never rounded down into the cap.
    assert first == 340


@pytest.mark.parametrize("arpu", [0, -1])
def test_a_cost_without_observed_arpu_is_refused_never_defaulted(arpu: int) -> None:
    with pytest.raises(OfferRefused, match="ARPU"):
        offer_cost(Bounds.load("default"), "percent_off", 20, 1, arpu, "USD")


def test_a_cost_without_a_currency_is_refused() -> None:
    with pytest.raises(OfferRefused, match="currency"):
        offer_cost(Bounds.load("default"), "percent_off", 20, 1, ANNUAL_ARPU, "")


def test_an_out_of_bounds_offer_has_no_cost() -> None:
    with pytest.raises(OfferRefused, match="percent_off"):
        offer_cost(Bounds.load("default"), "percent_off", 90, 1, ANNUAL_ARPU, "USD")


def test_total_cost_is_capped_at_a_fraction_of_value_at_risk() -> None:
    bounds = Bounds.load("default")

    # 5% of 1,000,000 is 50,000.
    assert bounds.check_cost(total_cost_cents=50_000, annual_value_at_risk_cents=1_000_000) is None
    reason = bounds.check_cost(total_cost_cents=50_001, annual_value_at_risk_cents=1_000_000)
    assert reason is not None
    assert "5%" in reason


def test_a_cap_against_no_value_at_risk_refuses() -> None:
    assert Bounds.load("default").check_cost(1, 0) is not None


def test_every_registered_profile_loads() -> None:
    # A profile in the file that does not parse is a bound nobody noticed was missing.
    for profile in ("default", "dev"):
        assert Bounds.load(profile).kinds


@pytest.mark.parametrize(
    ("kind", "amount", "months", "words"),
    [
        ("percent_off", 20, 3, "20% off for 3 months"),
        ("percent_off", 10, 1, "10% off for 1 month"),
        ("free_months", 2, 1, "2 free months"),
        ("free_months", 1, 1, "1 free month"),
        ("tier_upgrade", 1, 2, "tier upgrade for 2 months"),
        ("gift_card", 5, 1, "gift_card (5) for 1 month"),
    ],
)
def test_an_offer_is_described_in_words_a_person_weighs(
    kind: str, amount: int, months: int, words: str
) -> None:
    assert describe_offer(kind, amount, months) == words
