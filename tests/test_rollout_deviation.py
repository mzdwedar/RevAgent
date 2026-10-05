"""The model chooses the rollout percentage; the record and the bounds hold the rest (D3).

`rollout_deviation` holds the record-held fields (tenant, experiment, version, cohort
predicate, prior event) to the record and the percentage to policy bounds loaded from
config. A percentage outside the range, or an arm under the floor, is `OutOfBounds`; any
other difference is a deviation from the record. ADR-0015.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.context.frozen_cohorts import FrozenCohort
from agentstack.policy.offers import Bounds
from agentstack.runtime.drafting import (
    OutOfBounds,
    intended_rollout,
    rollout_deviation,
    rollout_instruction,
)
from agentstack.tools.experiments import prepare_rollout


def cohort(size: int = 4987) -> FrozenCohort:
    return FrozenCohort(
        tenant="acme",
        experiment_id="exp-7",
        experiment_version="exp:d39ddbec7cda1967",
        data_as_of="fixture:abc",
        targeting_model_version="tabpfn-3.5",
        risk_threshold=0.24,
        size=size,
        annual_value_at_risk_cents=1_000_000,
        description={},
    )


def proposed(percentage: int, frozen: FrozenCohort | None = None, **changes: Any) -> Any:
    frozen = frozen or cohort()
    return prepare_rollout(
        intended_rollout(frozen, prior_rollout_event=0, percentage=percentage) | changes
    )


@pytest.mark.parametrize("percentage", [5, 10, 15])
def test_any_percentage_in_the_range_is_admitted(percentage: int) -> None:
    assert rollout_deviation(proposed(percentage), cohort(), prior_rollout_event=0) is None


@pytest.mark.parametrize("percentage", [0, 4, 16, 100])
def test_a_percentage_outside_the_range_is_out_of_bounds(percentage: int) -> None:
    reason = rollout_deviation(proposed(percentage), cohort(), prior_rollout_event=0)

    assert isinstance(reason, OutOfBounds)
    assert "5" in reason and "15" in reason


def test_an_arm_under_the_floor_is_out_of_bounds_even_inside_the_range() -> None:
    small = cohort(size=1_000)  # 5% is 50 customers; the floor is 100

    reason = rollout_deviation(proposed(5, small), small, prior_rollout_event=0)

    assert isinstance(reason, OutOfBounds)
    assert "100" in reason
    assert rollout_deviation(proposed(10, small), small, prior_rollout_event=0) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"risk_threshold": 0.01},
        {"experiment_version": "exp:other"},
        {"targeting_model_version": "tabpfn-9"},
        {"prior_rollout_event": 3},
        {"experiment_id": "exp-8"},
    ],
)
def test_a_record_held_field_that_differs_is_a_deviation_not_out_of_bounds(
    changes: dict[str, Any],
) -> None:
    reason = rollout_deviation(proposed(10, **changes), cohort(), prior_rollout_event=0)

    assert reason is not None
    assert not isinstance(reason, OutOfBounds)


def test_a_deviation_outranks_a_bad_percentage() -> None:
    reason = rollout_deviation(proposed(100, risk_threshold=0.01), cohort(), prior_rollout_event=0)

    assert reason is not None
    assert not isinstance(reason, OutOfBounds)


def test_the_bounds_are_the_configs_not_the_instructions(tmp_path: Any) -> None:
    toml = tmp_path / "offers.toml"
    toml.write_text(
        "[profiles.p]\npercentage_min = 20\npercentage_max = 30\n"
        "minimum_treatment_arm = 1\ncost_cap_bps = 500\n"
        "[profiles.p.kinds.free_months]\n"
        "amount_min = 1\namount_max = 1\nmonths_min = 1\nmonths_max = 1\n"
        "cost_basis = 'months_of_arpu'\n"
    )

    reason = rollout_deviation(
        proposed(10), cohort(), prior_rollout_event=0, bounds=Bounds.load("p", toml)
    )

    assert isinstance(reason, OutOfBounds)


def test_the_instruction_states_the_range_the_arm_and_asks_for_a_reason() -> None:
    told = rollout_instruction(experiment_id="exp-7", cohort=cohort(), prior_rollout_event=0)

    assert "5" in told and "15" in told
    assert "100 customers" in told
    assert "reason" in told
    assert "exactly as given" in told
    assert "percentage=" not in told  # the model chooses it; it is not dictated


def test_the_instruction_still_dictates_the_record_held_arguments() -> None:
    frozen = cohort()
    told = rollout_instruction(experiment_id="exp-7", cohort=frozen, prior_rollout_event=2)

    for token in (
        "tenant=acme",
        "experiment_id=exp-7",
        f"experiment_version={frozen.experiment_version}",
        "targeting_model_version=tabpfn-3.5",
        "risk_threshold=0.24",
        "prior_rollout_event=2",
    ):
        assert token in told


def test_the_instruction_raises_the_lower_end_for_a_small_cohort() -> None:
    # On 1,000 customers 5% is 50 < 100: the lowest percentage that is allowed is 10.
    told = rollout_instruction(
        experiment_id="exp-7", cohort=cohort(size=1_000), prior_rollout_event=0
    )

    assert "10" in told and "15" in told


# --- D4: the reason travels in the bound action, so the person is shown what they bind ---


def test_a_reason_is_in_the_payload_and_in_the_fingerprint() -> None:
    plain = proposed(10)
    reasoned = proposed(10, reason="small arm first")
    reworded = proposed(10, reason="large arm first")

    assert reasoned.payload["reason"] == "small arm first"
    assert "reason" not in plain.payload
    assert len({plain.fingerprint(), reasoned.fingerprint(), reworded.fingerprint()}) == 3


def test_a_reason_is_not_a_deviation() -> None:
    request = proposed(10, reason="small arm first")

    assert rollout_deviation(request, cohort(), prior_rollout_event=0) is None


def test_a_reason_that_is_not_text_is_a_deviation() -> None:
    request = proposed(10, reason=["not", "text"])

    assert rollout_deviation(request, cohort(), prior_rollout_event=0) is not None


def test_the_allowed_range_is_the_configs_raised_by_the_arm_floor() -> None:
    bounds = Bounds.load("default")

    assert bounds.allowed_percentages(10_000) == (5, 15)
    assert bounds.allowed_percentages(1_000) == (10, 15)
