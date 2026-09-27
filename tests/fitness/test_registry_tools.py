"""The registry tools are narrow, and each stage is shown only its own (SPEC-registry.md).

A tool's blast radius is bounded twice: by its shape, and by which runs are shown it at
all. This file holds the second bound as an exact matrix - not "rollout is absent from
drafting" but "drafting is shown precisely these tools" - because an assertion of
absence passes just as happily when a new, wider tool is added beside it.

The matrix grows one row at a time as the registry tools land (T26-T29).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from agentstack.execution.gateway import UnresolvedEffect
from agentstack.execution.surfaces import (
    PostgresRegistryClient,
    SurfaceClient,
    SurfaceTimeout,
)
from agentstack.interfaces.wiring import Stack, envelope_for
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import new_run
from agentstack.storage.database import Database
from agentstack.tools.action import ActionRequest
from agentstack.tools.catalog import build_registry
from agentstack.tools.experiments import (
    DRAFT_STAGE,
    EVALUATION_STAGE,
    LIST,
    ROLLOUT_STAGE,
    STATUSES,
    prepare_abstain,
    prepare_discard,
    prepare_halt,
    prepare_list,
    prepare_revise,
)
from agentstack.tools.spec import Approval, Surface

from .conftest import TENANT, USER

READS = {"get_experiment", "list_experiments"}

EXPOSURE = {
    DRAFT_STAGE: READS
    | {"create_experiment_draft", "revise_draft_hypothesis", "discard_experiment_draft"},
    EVALUATION_STAGE: READS | {"get_rollout_history", "record_abstention", "halt_rollout"},
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


# --- spec criterion 5: an answer lost after the effect is still one effect ---

REGISTRY_SCOPES = frozenset(
    {
        "experiments:read",
        "experiments:draft",
        "experiments:annotate",
        "experiments:halt",
        "experiments:rollout",
    }
)
EXPERIMENT_AT = f"{TENANT}/experiments/exp-7"
VERSION = "exp:abc123"
SEED_DRAFT = {
    "experiment_version": VERSION,
    "hypothesis": "a discount retains at-risk customers",
    "variant": "20-percent-off",
}
SEED_ROLLOUT = {
    "experiment_version": VERSION,
    "percentage": 10,
    "targeting_model_version": "tabpfn-3.5",
    "risk_threshold": 0.61,
}


@dataclass(frozen=True)
class AnswerLost:
    """The real store, with the answer dropped after every write has applied."""

    inner: SurfaceClient

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        return self.inner.read(resource, query)

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        self.inner.commit(resource, payload)
        raise SurfaceTimeout(f"{resource}: applied, answer lost")


Prepare = Callable[[Mapping[str, Any]], ActionRequest]

# Every registry write that can be retried, its own arguments, and the state it acts on.
WRITES: dict[str, tuple[Prepare, dict[str, Any], str]] = {
    "revise_draft_hypothesis": (
        prepare_revise,
        {"hypothesis": "a smaller discount retains them too"},
        "draft",
    ),
    "discard_experiment_draft": (prepare_discard, {"reason": "the cohort is too small"}, "draft"),
    "record_abstention": (
        prepare_abstain,
        {"explanation": "the guardrail metric is inside its noise band"},
        "live",
    ),
    "halt_rollout": (prepare_halt, {"reason": "churn rose in the variant"}, "live"),
}


def _history_rows(db: Database) -> int:
    row = db.fetch_one(
        "SELECT (SELECT count(*) FROM draft_revisions) + (SELECT count(*) FROM registry_events)"
    )
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("tool", sorted(WRITES))
def test_a_lost_answer_and_a_retry_leave_one_row(
    stack: Stack, app_database: Database, tool: str
) -> None:
    """Applied, answer lost, retried: the retry is refused as unresolved and the store
    holds one row, not two - against the real store, not the fake."""
    prepare, arguments, state = WRITES[tool]
    stack.registry_client.commit(EXPERIMENT_AT, SEED_DRAFT)
    if state == "live":
        stack.registry_client.commit(f"{EXPERIMENT_AT}/rollout", SEED_ROLLOUT)
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    run = stack.runs.ensure(
        new_run(session_id=session.session_id, tenant=TENANT, user=USER, channel="test")
    )
    view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
    request = prepare(
        {"tenant": TENANT, "experiment_id": "exp-7", "experiment_version": VERSION, **arguments}
    )
    before = _history_rows(app_database)

    def attempt() -> None:
        stack.deps.gateway.execute(
            request=request,
            spec=build_registry().spec(tool),
            envelope=envelope_for(view, scopes=REGISTRY_SCOPES),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=Tracer(
                run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions
            ),
        )

    stack.deps.gateway.surfaces[Surface.REGISTRY] = AnswerLost(stack.registry_client)
    with pytest.raises(UnresolvedEffect):
        attempt()
    stack.deps.gateway.surfaces[Surface.REGISTRY] = stack.registry_client
    with pytest.raises(UnresolvedEffect, match="(?i)reconcile"):
        attempt()

    assert _history_rows(app_database) == before + 1
