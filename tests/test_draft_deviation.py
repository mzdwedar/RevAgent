"""A draft is held to the frozen cohort it is about (Checkpoint B).

The drafting turn is told `experiment_version=...` "exactly as given". qwen3:8b copied it
with one character added, and the draft landed in the registry under a version no cohort
was frozen for. The model writes the hypothesis and the variant; it does not choose which
cohort they belong to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentstack.context.frozen_cohorts import FrozenCohort
from agentstack.policy.offers import Bounds
from agentstack.runtime.drafting import OutOfBounds, draft_deviation
from agentstack.tools.experiments import DRAFT, prepare_draft

COHORT = FrozenCohort(
    tenant="acme",
    experiment_id="exp-7",
    experiment_version="exp:d39ddbec7cda1967",
    data_as_of="fixture:abc",
    targeting_model_version="tabpfn-3.5",
    risk_threshold=0.24,
    size=4987,
    annual_value_at_risk_cents=1_000_000,
    description={},
)


def drafted(**changes: Any) -> Any:
    arguments = {
        "tenant": COHORT.tenant,
        "experiment_id": COHORT.experiment_id,
        "experiment_version": COHORT.experiment_version,
        "hypothesis": "A 90-day plan offer retains customers who churn at renewal.",
        "variant": "90-day plan",
        "offer_kind": "percent_off",
        "offer_amount": 20,
        "offer_months": 3,
    } | changes
    return prepare_draft(arguments)


def test_a_draft_of_the_frozen_cohort_is_admitted() -> None:
    assert draft_deviation(drafted(), COHORT) is None


def test_the_hypothesis_and_the_variant_are_the_models_to_write() -> None:
    assert draft_deviation(drafted(hypothesis="anything", variant="at all"), COHORT) is None


def test_a_version_copied_with_one_character_added_is_refused() -> None:
    reason = draft_deviation(drafted(experiment_version="exp:d39ddbec7c7da1967"), COHORT)
    assert reason is not None
    assert "exp:d39ddbec7c7da1967" in reason and COHORT.experiment_version in reason


@pytest.mark.parametrize(
    "changes",
    [{"experiment_id": "exp-8"}, {"tenant": "other"}],
    ids=["another-experiment", "another-tenant"],
)
def test_a_draft_of_another_experiment_is_refused(changes: dict[str, str]) -> None:
    assert draft_deviation(drafted(**changes), COHORT) is not None


def test_only_the_draft_tool_drafts() -> None:
    request = drafted()
    other = type(request)(
        tool="roll_out_variant_to_percentage",
        surface=request.surface,
        resource=request.resource,
        payload=request.payload,
        idempotency_key=request.idempotency_key,
    )
    assert DRAFT.name != other.tool
    assert draft_deviation(other, COHORT) is not None


# --- bounded discretion (ADR-0015): the model chooses the offer, policy holds the range ---


@pytest.mark.parametrize(
    ("kind", "amount", "months"),
    [("percent_off", 10, 1), ("percent_off", 50, 3), ("free_months", 2, 1), ("tier_upgrade", 1, 3)],
)
def test_an_offer_inside_the_catalog_is_admitted(kind: str, amount: int, months: int) -> None:
    request = drafted(offer_kind=kind, offer_amount=amount, offer_months=months)

    assert draft_deviation(request, COHORT) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"offer_kind": "gift_card"},
        {"offer_amount": 90},
        {"offer_amount": 9},
        {"offer_months": 12},
        {"offer_kind": "free_months", "offer_amount": 6, "offer_months": 1},
    ],
    ids=["unknown-kind", "too-generous", "too-small", "too-long", "too-many-free-months"],
)
def test_an_offer_outside_the_catalog_is_refused_as_out_of_bounds(changes: dict[str, Any]) -> None:
    reason = draft_deviation(drafted(**changes), COHORT)

    assert isinstance(reason, OutOfBounds)
    assert changes.get("offer_kind", "percent_off") in reason


def test_a_total_cost_over_the_cap_is_refused_whatever_the_catalog_says(tmp_path: Path) -> None:
    # The signed-off catalog cannot reach 5% of value at risk, so the cap is shown binding
    # with a tighter one: 1 basis point. 20% off for 3 months at 200 a year is 10 a
    # customer, over the 15% arm the policy allows (748 customers).
    toml = tmp_path / "offers.toml"
    toml.write_text(
        "[profiles.p]\npercentage_min = 5\npercentage_max = 15\n"
        "minimum_treatment_arm = 100\ncost_cap_bps = 1\n"
        "[profiles.p.kinds.percent_off]\n"
        "amount_min = 10\namount_max = 50\nmonths_min = 1\nmonths_max = 3\n"
        "cost_basis = 'percent_of_arpu_per_month'\n"
    )

    reason = draft_deviation(drafted(), COHORT, bounds=Bounds.load("p", toml))

    assert isinstance(reason, OutOfBounds)
    assert "cap" in reason


def test_a_wrong_version_is_a_deviation_not_an_out_of_bounds() -> None:
    reason = draft_deviation(drafted(experiment_version="exp:other", offer_amount=90), COHORT)

    assert reason is not None
    assert not isinstance(reason, OutOfBounds)


def test_the_draft_stores_the_offer_and_the_arguments_carry_no_cost() -> None:
    # Cost is first-party: computed by the store from the frozen cohort, never an argument.
    payload = drafted().payload

    assert (payload["offer_kind"], payload["offer_amount"], payload["offer_months"]) == (
        "percent_off",
        20,
        3,
    )
    assert not {"cost", "total_cost_cents", "cost_per_customer_cents"} & set(payload)
