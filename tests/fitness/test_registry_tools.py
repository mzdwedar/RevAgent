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
from agentstack.tools.experiments import (
    DRAFT_STAGE,
    EVALUATION_STAGE,
    LIST,
    ROLLOUT_STAGE,
    STATUSES,
    prepare_list,
)
from agentstack.tools.spec import Approval, Surface

from .conftest import TENANT

READS = {"get_experiment", "list_experiments"}

EXPOSURE = {
    DRAFT_STAGE: READS | {"create_experiment_draft"},
    EVALUATION_STAGE: READS | {"get_rollout_history"},
    ROLLOUT_STAGE: READS | {"get_rollout_history", "roll_out_variant_to_percentage"},
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


def test_a_read_is_a_read() -> None:
    """No effect, no approval, one scope: a read that needed a tier would be a write
    wearing a read's name, and one that took a write scope would widen every grant."""
    reads = [s for s in build_registry().specs() if s.name in READS | {"get_rollout_history"}]

    assert len(reads) == 3
    for spec in reads:
        assert spec.side_effecting is False
        assert spec.approval is Approval.NONE
        assert spec.scope == "experiments:read"
        assert spec.surface is Surface.REGISTRY


def test_a_list_is_bounded_in_its_schema() -> None:
    limit = LIST.input_schema["properties"]["limit"]

    assert (limit["minimum"], limit["maximum"]) == (1, 50)
    assert LIST.input_schema["properties"]["status"]["enum"] == STATUSES


def test_the_collection_read_stays_inside_the_sandbox_prefix() -> None:
    """Listing names the collection with a trailing slash, which the existing
    `{tenant}/experiments/` prefix already covers - containment is not widened."""
    request = prepare_list({"tenant": TENANT})

    assert request.resource == f"{TENANT}/experiments/"
    assert request.payload == {"limit": 20}


@pytest.mark.parametrize(
    ("tool", "arguments", "resource"),
    [
        ("get_experiment", {"experiment_id": "exp-7"}, f"{TENANT}/experiments/exp-7"),
        ("get_rollout_history", {"experiment_id": "exp-7"}, f"{TENANT}/experiments/exp-7/history"),
        ("list_experiments", {"status": "live", "limit": 5}, f"{TENANT}/experiments/"),
    ],
)
def test_each_read_asks_the_surface_through_a_resource_it_serves(
    stack: Stack, tool: str, arguments: dict[str, object], resource: str
) -> None:
    """Prepared through the registry, validated, and read back through the real store:
    a read tool whose resource the surface refused would be a menu item that never works."""
    registry = build_registry()
    exposed = registry.expose_for(tenant=TENANT, stage=ROLLOUT_STAGE)

    request = registry.prepare(tool, {"tenant": TENANT, **arguments}, exposed=exposed)

    assert request.resource == resource
    assert isinstance(stack.registry_client.read(request.resource, request.payload), dict)
