"""Parts 4 and 7: no tool's own effect moves the snapshot its approval is checked against.

An approval binds to a state snapshot, and a stale one is refused. A rerun of an act
whose answer was lost (the process died between the surface applying the effect and
the step completing) is supposed to deduplicate: the gateway checks the approval, then
the ledger hands back the first attempt's receipt. That only works if the rerun computes
the same snapshot as the first attempt did. If the effect itself moved it, the rerun
reads its own grant as stale *before* the ledger is consulted, and parks a question
about an act that already happened (audit finding H1: a draft creates the experiment the
registry then describes, so "before" was `None` and "after" was a description).

So this is asked of every side-effecting tool in the catalog, not of the draft: take the
snapshot the turn would, apply the tool's own effect on its surface, take it again. A new
tool that moves its own snapshot fails here, before it can wedge a run.

The other half is held too: the action a person approves is still bound to the world at
the act, so the fix is not "stop looking".
"""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.execution.surfaces import RecordingClient, RegistryClient
from agentstack.runtime.snapshot import approval_snapshot, binds_the_world
from agentstack.tools.action import ActionRequest
from agentstack.tools.catalog import build_registry
from agentstack.tools.experiments import DRAFT, HALT, ROLLOUT
from agentstack.tools.spec import Approval, Surface, ToolSpec

TENANT = "acme"
CONTEXT = "fp-context"

REGISTRY = build_registry()
EFFECTS = tuple(spec for spec in REGISTRY.specs() if spec.side_effecting)


def _surfaces() -> dict[Surface, Any]:
    """A fresh fake per surface a tool can name. A tool on a surface with no fake here
    fails loudly: the question can't be answered for it, which is not the same as yes."""
    return {Surface.API: RecordingClient(), Surface.REGISTRY: RegistryClient()}


def _arguments(spec: ToolSpec) -> dict[str, Any]:
    """Arguments valid against the tool's schema, the same across tools, so a tool
    registered earlier sets up the resource a later one acts on (a draft, then its
    rollout)."""
    made: dict[str, Any] = {}
    for name, schema in spec.input_schema["properties"].items():
        kind = schema.get("type")
        if name == "tenant":
            made[name] = TENANT
        elif name in ("prior_rollout_event", "prior_event"):
            # These are the registry's own compare-and-set key (C2): the fresh fake
            # this file builds the world in starts with nothing recorded, so the move
            # that follows it names 0, not the placeholder every other integer gets.
            made[name] = 0
        elif name == "prior_revision":
            # A fresh draft's own revision list holds one entry (the draft's own
            # hypothesis), so the first revision past it names 1.
            made[name] = 1
        elif kind == "integer":
            made[name] = 10
        elif kind == "number":
            made[name] = 0.5
        else:
            made[name] = f"{name.replace('_', '-')}-1"
    return made


def _prepared(spec: ToolSpec) -> ActionRequest:
    return REGISTRY.prepare(spec.name, _arguments(spec), exposed=(spec,))


def _snapshot(spec: ToolSpec, request: ActionRequest, surfaces: dict[Surface, Any]) -> str:
    """What `nodes.act` computes for this proposal, with the surface as the world.

    `committed` is what the run recorded against the resource *before* this act: on a
    rerun after the answer was lost, the step never completed, so it is the same list
    both times.
    """

    def observe() -> Any:
        return surfaces[request.surface].state(request.resource)

    return approval_snapshot(
        spec=spec,
        request=request,
        observe=observe,
        context_fingerprint=CONTEXT,
        committed=(),
    )


def _world_before(spec: ToolSpec) -> dict[Surface, Any]:
    """The world in which this tool's precondition holds.

    Not every effect registered before this one - the catalog is a menu, not a
    sequence, and revising, discarding, rolling out and halting are divergent branches
    from one draft, not consecutive steps. The only true prerequisites are the draft
    itself, always, and a rollout for the one spec that needs a live experiment rather
    than a draft (a halt)."""
    surfaces = _surfaces()
    prerequisites = (DRAFT, ROLLOUT) if spec.name == HALT.name else (DRAFT,)
    for earlier in prerequisites:
        if earlier.name == spec.name:
            continue
        request = _prepared(earlier)
        if request.surface is spec.surface:
            surfaces[request.surface].commit(request.resource, request.payload)
    return surfaces


def test_the_catalog_has_effects_to_ask_about() -> None:
    """An empty parametrisation passes vacuously; this says what it covers."""
    names = {spec.name for spec in EFFECTS}
    assert {"create_experiment_draft", "roll_out_variant_to_percentage", "issue_refund"} <= names


@pytest.mark.parametrize("spec", EFFECTS, ids=lambda spec: spec.name)
def test_a_tools_own_effect_does_not_move_its_approval_snapshot(spec: ToolSpec) -> None:
    surfaces = _world_before(spec)
    assert spec.surface in surfaces, f"no fake for {spec.surface.value}; add one to answer this"
    request = _prepared(spec)

    before = _snapshot(spec, request, surfaces)
    surfaces[request.surface].commit(request.resource, request.payload)
    rerun = _snapshot(spec, request, surfaces)

    assert rerun == before, (
        f"{spec.name}'s own effect moved the snapshot its approval is checked against: "
        "a rerun after a lost answer would read its own grant as stale and park, instead "
        "of deduplicating"
    )


def test_an_action_a_person_approves_is_still_bound_to_the_world() -> None:
    """The rollout's approval is checked against the experiment as the registry
    describes it at the act. Revise the hypothesis after the approval, and the snapshot
    moves: the approval is stale, as ADR-0008 rule 3 requires."""
    (rollout,) = (s for s in EFFECTS if s.name == "roll_out_variant_to_percentage")
    assert binds_the_world(rollout)
    surfaces = _world_before(rollout)
    request = _prepared(rollout)
    approved = _snapshot(rollout, request, surfaces)

    registry = surfaces[Surface.REGISTRY]
    registry.drafts[f"{TENANT}/experiments/experiment-id-1"]["hypothesis"] = "a different offer"

    assert _snapshot(rollout, request, surfaces) != approved


@pytest.mark.parametrize("spec", REGISTRY.specs(), ids=lambda spec: spec.name)
def test_only_what_a_person_approves_is_bound_to_the_world(spec: ToolSpec) -> None:
    assert binds_the_world(spec) is (spec.approval is Approval.ALWAYS)
