"""The registry tools are narrow, and each stage is shown only its own (SPEC-registry.md).

A tool's blast radius is bounded twice: by its shape, and by which runs are shown it at
all. This file holds the second bound as an exact matrix - not "rollout is absent from
drafting" but "drafting is shown precisely these tools" - because an assertion of
absence passes just as happily when a new, wider tool is added beside it.

The matrix grows one row at a time as the registry tools land (T26-T29).
"""

from __future__ import annotations

import pytest

from agentstack.execution.surfaces import PostgresRegistryClient
from agentstack.interfaces.wiring import Stack
from agentstack.tools.catalog import build_registry
from agentstack.tools.experiments import DRAFT_STAGE, EVALUATION_STAGE, ROLLOUT_STAGE

from .conftest import TENANT

EXPOSURE = {
    DRAFT_STAGE: {"create_experiment_draft"},
    EVALUATION_STAGE: set(),
    ROLLOUT_STAGE: {"roll_out_variant_to_percentage"},
}


@pytest.mark.parametrize("stage", sorted(EXPOSURE))
def test_each_experiment_stage_is_shown_exactly_its_own_tools(stage: str) -> None:
    names = {spec.name for spec in build_registry().expose_for(tenant=TENANT, stage=stage)}

    assert names == EXPOSURE[stage]


def test_a_drafting_turn_cannot_see_the_rollout_tool() -> None:
    """The gap this task closed: drafting and rolling out shared one stage, so a turn
    writing a reversible draft was also offered the one irreversible act."""
    names = {s.name for s in build_registry().expose_for(tenant=TENANT, stage=DRAFT_STAGE)}

    assert "roll_out_variant_to_percentage" not in names


def test_the_experiment_stages_are_distinct() -> None:
    assert len({DRAFT_STAGE, EVALUATION_STAGE, ROLLOUT_STAGE}) == 3


def test_the_stack_writes_the_real_registry(stack: Stack) -> None:
    """The fake is for tests that ask for it. A stack wired from `build_stack` records
    drafts and rollouts where they outlive the process - T25's whole point."""
    assert isinstance(stack.registry_client, PostgresRegistryClient)
