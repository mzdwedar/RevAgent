"""A draft is held to the frozen cohort it is about (Checkpoint B).

The drafting turn is told `experiment_version=...` "exactly as given". qwen3:8b copied it
with one character added, and the draft landed in the registry under a version no cohort
was frozen for. The model writes the hypothesis and the variant; it does not choose which
cohort they belong to.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.context.frozen_cohorts import FrozenCohort
from agentstack.runtime.drafting import draft_deviation
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
