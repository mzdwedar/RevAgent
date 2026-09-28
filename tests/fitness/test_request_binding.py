"""A request is held to the spec it is authorised under (C1, H2).

Two findings, one shape. The registry reads the act a resource names from its last
segment, so:

* **C1** - an id is part of the path. `experiment_id=exp-9/rollout` passed a schema that
  declared `format: id` and enforced nothing, and a *drafting* turn rolled exp-9 out
  under the draft tool's `PRE_COMMIT` grant, with no human.
* **H2** - the gateway judged the spec and trusted the request to be one of its requests.
  A rollout request presented beside the abstention spec was checked for the annotate
  scope, granted by the abstention's rule, and committed at 100%.

Each invariant below is held over the whole catalog, not over a list typed here, so a
new tool is held to it the moment it is registered.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agentstack.execution.surfaces import RegistryClient, resource_verb
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.policy.decisions import PolicyDenied, bound_to
from agentstack.runtime.run import Run, new_run
from agentstack.storage.database import Database
from agentstack.tools.action import ActionRequest
from agentstack.tools.catalog import build_registry
from agentstack.tools.experiments import (
    ABSTAIN,
    DRAFT,
    DRAFT_STAGE,
    GET,
    HALT,
    prepare_abstain,
    prepare_draft,
    prepare_get,
    prepare_halt,
    prepare_rollout,
)
from agentstack.tools.spec import ActsAs, Approval, Idempotency, Surface, ToolSpec
from agentstack.tools.validation import InvalidToolArguments, known_formats, validate_arguments

from .conftest import TENANT, USER

REGISTRY = build_registry()
SPECS = REGISTRY.specs()
REGISTRY_SCOPES = frozenset(
    {
        "experiments:read",
        "experiments:draft",
        "experiments:annotate",
        "experiments:halt",
        "experiments:rollout",
    }
)
VERSION = "exp:abc123"
DRAFTED = {
    "experiment_version": VERSION,
    "hypothesis": "a discount retains at-risk customers",
    "variant": "20-percent-off",
}


def valid_arguments(spec: ToolSpec) -> dict[str, Any]:
    """Arguments that pass `spec`'s schema, derived from the schema itself. Each string
    is a distinct marker, so where it lands in the prepared resource can be seen."""
    arguments: dict[str, Any] = {}
    for index, (name, declared) in enumerate(spec.input_schema["properties"].items()):
        if "enum" in declared:
            arguments[name] = declared["enum"][0]
        elif declared["type"] == "string":
            arguments[name] = f"m{index}x"
        elif declared["type"] == "integer":
            arguments[name] = declared.get("minimum", 1)
        else:
            arguments[name] = 0.5
    return arguments


def prepared(spec: ToolSpec, arguments: dict[str, Any] | None = None) -> ActionRequest:
    return REGISTRY.prepare(spec.name, arguments or valid_arguments(spec), exposed=SPECS)


def path_arguments(spec: ToolSpec) -> list[str]:
    """The arguments whose value becomes a segment of the prepared resource."""
    arguments = valid_arguments(spec)
    segments = prepared(spec, arguments).resource.split("/")
    return [name for name, value in arguments.items() if value in segments]


# --- C1: an id is one segment ---


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_every_id_that_reaches_a_resource_path_is_one_segment(spec: ToolSpec) -> None:
    """Whatever argument ends up in the path, a value that would add a segment - and so
    choose the act the surface reads from the last one - is refused by validation."""
    names = path_arguments(spec)
    assert "tenant" in names, f"{spec.name}: its resource does not start from the tenant"

    for name in names:
        assert spec.input_schema["properties"][name].get("format") == "id", (
            f"{spec.name}.{name} becomes part of a resource path and declares no id format"
        )
        for hostile in ("exp-9/rollout", "exp-9/halt", "../globex", "..", "", "a b"):
            with pytest.raises(InvalidToolArguments, match=name):
                validate_arguments(spec, {**valid_arguments(spec), name: hostile})


def test_every_declared_format_is_one_the_validator_enforces() -> None:
    """A format nothing checks is decoration, which is what `id` was. The validator
    refuses an unknown format; this is what keeps that refusal from firing on a tool."""
    declared = {
        prop["format"]
        for spec in SPECS
        for prop in spec.input_schema["properties"].values()
        if "format" in prop
    }

    assert declared <= known_formats()


def test_an_undeclared_format_is_refused_rather_than_waved_through() -> None:
    spec = replace(
        GET,
        input_schema={
            "type": "object",
            "properties": {"tenant": {"type": "string", "format": "uuid"}},
            "required": ["tenant"],
        },
    )

    with pytest.raises(InvalidToolArguments, match="nothing enforces"):
        validate_arguments(spec, {"tenant": TENANT})


def test_an_id_that_is_one_segment_still_passes() -> None:
    assert validate_arguments(GET, {"tenant": "acme", "experiment_id": "exp_7.v2:b-1"}) == {
        "tenant": "acme",
        "experiment_id": "exp_7.v2:b-1",
    }


# --- the verb a spec declares is the verb its own prepare produces ---


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_each_tool_prepares_the_verb_it_declares(spec: ToolSpec) -> None:
    """Read through the surface's own parser, so the spec, the prepare and the client
    agree on what the resource means - or this fails."""
    request = prepared(spec)

    verb = resource_verb(request.surface, request.resource, writes=spec.side_effecting)
    assert verb == spec.verb
    assert (spec.verb is not None) == (spec.surface is Surface.REGISTRY)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_an_honest_request_is_bound_to_its_own_spec(spec: ToolSpec) -> None:
    """The binding check refuses nothing a tool's own prepare builds - including every
    value a spec fixes, which the prepare writes."""
    request = prepared(spec)
    verb = resource_verb(request.surface, request.resource, writes=spec.side_effecting)

    assert bound_to(spec, request, verb=verb) is None
    for key, fixed in spec.fixed_payload.items():
        assert request.payload[key] == fixed.value


def test_a_registry_tool_cannot_be_declared_without_a_verb() -> None:
    with pytest.raises(ValueError, match="declares the verb"):
        ToolSpec(
            name="touch_experiment",
            description="do something to an experiment",
            input_schema={"type": "object"},
            acts_as=ActsAs.DELEGATED,
            scope="experiments:read",
            surface=Surface.REGISTRY,
            side_effecting=False,
            reversible=True,
            approval=Approval.NONE,
            idempotency=Idempotency.NATURAL,
        )


def test_the_halt_fixes_its_percentage_in_the_spec() -> None:
    assert {key: fixed.value for key, fixed in HALT.fixed_payload.items()} == {"percentage": 0}


# --- H2: the gateway refuses a request that is not the spec's ---


def ids(experiment: str = "exp-9") -> dict[str, str]:
    return {"tenant": TENANT, "experiment_id": experiment, "experiment_version": VERSION}


ABSTENTION = prepare_abstain({**ids(), "explanation": "inside the noise band"})
ROLLOUT_AT_100 = prepare_rollout(
    {**ids(), "percentage": 100, "targeting_model_version": "tabpfn-3.5", "risk_threshold": 0.61}
)
HALT_REQUEST = prepare_halt({**ids(), "reason": "churn rose"})

MISMATCHES: dict[str, tuple[ToolSpec, ActionRequest, str]] = {
    # A rollout request, handed in beside the abstention spec.
    "tool": (ABSTAIN, ROLLOUT_AT_100, "binding.tool"),
    # The abstention's own request, pointed at another surface.
    "surface": (ABSTAIN, replace(ABSTENTION, surface=Surface.API), "binding.surface"),
    # The audit's reproduction: the abstention's tool name, a rollout's resource and
    # payload. Every check `decide` makes passes for it.
    "verb": (
        ABSTAIN,
        replace(
            ABSTENTION,
            resource=f"{TENANT}/experiments/exp-9/rollout",
            payload=ROLLOUT_AT_100.payload,
        ),
        "binding.verb",
    ),
    # A resource no verb serves at all is no verb, not a guess.
    "no verb": (
        ABSTAIN,
        replace(ABSTENTION, resource=f"{TENANT}/experiments/exp-9/delete_everything"),
        "binding.verb",
    ),
    "a halt to 5%": (
        HALT,
        replace(HALT_REQUEST, payload={**HALT_REQUEST.payload, "percentage": 5}),
        "binding.payload",
    ),
    # `False == 0` and `0.0 == 0` in Python; neither is the zero a halt fixes.
    "a halt to False": (
        HALT,
        replace(HALT_REQUEST, payload={**HALT_REQUEST.payload, "percentage": False}),
        "binding.payload",
    ),
    "a halt to 0.0": (
        HALT,
        replace(HALT_REQUEST, payload={**HALT_REQUEST.payload, "percentage": 0.0}),
        "binding.payload",
    ),
    "a halt that names no percentage": (
        HALT,
        replace(HALT_REQUEST, payload={"experiment_version": VERSION, "reason": "churn rose"}),
        "binding.payload",
    ),
}


def tracer_for(stack: Stack, run: Run) -> Tracer:
    return Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)


def fake_registry(stack: Stack, live: bool = True) -> RegistryClient:
    """An inspectable registry holding exp-9, swapped in so "the surface was never
    asked" is a list that stayed empty rather than an inference."""
    fake = RegistryClient()
    fake.commit(f"{TENANT}/experiments/exp-9", DRAFTED)
    if live:
        fake.commit(
            f"{TENANT}/experiments/exp-9/rollout", {**ROLLOUT_AT_100.payload, "percentage": 10}
        )
    stack.deps.gateway.surfaces[Surface.REGISTRY] = fake
    return fake


@pytest.mark.parametrize("case", sorted(MISMATCHES))
def test_a_request_that_is_not_the_specs_is_refused_before_policy_and_audited(
    stack: Stack, run: Run, case: str
) -> None:
    spec, request, rule = MISMATCHES[case]
    fake = fake_registry(stack)
    events, reads = list(fake.events), list(fake.reads)
    view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
    # A person approving exactly this request changes nothing: binding runs before
    # approval is consulted, as well as before `decide`.
    stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot="s",
        approver="a distracted human",
        summary="looked fine",
    )
    tracer = tracer_for(stack, run)

    with pytest.raises(PolicyDenied):
        stack.deps.gateway.execute(
            request=request,
            spec=spec,
            envelope=envelope_for(view, scopes=REGISTRY_SCOPES),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=tracer,
        )

    assert (fake.events, fake.reads) == (events, reads), "the registry was asked"
    assert stack.client.calls == [], "another surface was asked"
    assert fake.read(f"{TENANT}/experiments/exp-9", {})["status"] == "live"
    denied = stack.audit.for_run(run.run_id)
    assert [(r.policy_decision, r.outcome) for r in denied] == [(rule, "denied")]
    assert "approval.request" not in tracer.names(), "approval was consulted"


def test_a_read_is_bound_to_its_verb_too(stack: Stack, run: Run) -> None:
    """A read of `/history` presented under `get_experiment` is refused. A read has no
    effect to undo, which is why it is cheap to be exact about - and a read spec that
    reached a write resource is refused the same way."""
    fake = fake_registry(stack, live=False)
    view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
    get = prepare_get(ids())

    for spec, request in (
        (GET, replace(get, resource=f"{get.resource}/history")),
        (DRAFT, prepare_draft({**ids(), "hypothesis": "h", "variant": "v"})),
    ):
        with pytest.raises(PolicyDenied):
            stack.deps.gateway.read(
                request=request,
                spec=spec,
                envelope=envelope_for(view, scopes=REGISTRY_SCOPES),
                run_id=run.run_id,
                state_snapshot="s",
                tracer=tracer_for(stack, run),
            )

    assert fake.reads == []
    # The second is the draft's own request and spec: read, its resource is the
    # experiment, not a draft - the verb is taken for the act actually asked for.
    assert [r.policy_decision for r in stack.audit.for_run(run.run_id)] == [
        "binding.verb",
        "binding.verb",
    ]


def test_the_c1_request_is_refused_by_binding_even_past_validation(stack: Stack, run: Run) -> None:
    """Defence in depth: the draft tool's own prepare, fed the id validation now refuses,
    builds a resource that names a rollout. The gateway refuses it for its verb."""
    fake = fake_registry(stack, live=False)
    view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
    request = prepare_draft({**ids("exp-9/rollout"), "hypothesis": "h", "variant": "v"})

    with pytest.raises(PolicyDenied, match="create_experiment_draft is a draft"):
        stack.deps.gateway.execute(
            request=request,
            spec=DRAFT,
            envelope=envelope_for(view, scopes=frozenset({"experiments:draft"})),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=tracer_for(stack, run),
        )

    assert fake.events == []
    assert fake.read(f"{TENANT}/experiments/exp-9", {})["status"] == "draft"


# --- C1, through the whole turn ---


def test_a_drafting_turn_cannot_roll_out_through_an_experiment_id(
    stack: Stack, app_database: Database
) -> None:
    """The audit's exploit, as the model would propose it: on a draft-stage run, draft
    `exp-9/rollout`. Refused by validation, before the registry; exp-9 is still a draft,
    and no exposure event exists - so there is nothing for `get_rollout_history` to
    choke on either."""
    stack.registry_client.commit(f"{TENANT}/experiments/exp-9", DRAFTED)
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    run = stack.runs.ensure(
        new_run(
            session_id=session.session_id,
            tenant=TENANT,
            user=USER,
            stage=DRAFT_STAGE,
            channel="test",
        )
    )
    event = InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=run.session_id,
        text=(
            "create_experiment_draft tenant=acme experiment_id=exp-9/rollout "
            f"experiment_version={VERSION} hypothesis=bigger variant=100-percent-off"
        ),
    )

    result = handle(
        stack, event, scopes=frozenset({"experiments:read", "experiments:draft"}), run=run
    )

    assert result.status == "rejected"
    assert "tool.reject" in result.tracer.names()
    assert "experiment_id" in result.text
    assert app_database.fetch_one("SELECT count(*) FROM registry_events") == (0,)
    assert stack.registry_client.read(f"{TENANT}/experiments/exp-9", {})["status"] == "draft"
    assert stack.registry_client.read(f"{TENANT}/experiments/exp-9/history", {}) == {
        "experiment_id": "exp-9",
        "events": [],
        "current_exposure": 0,
    }
    assert not [r for r in stack.audit.for_run(run.run_id) if r.outcome == "committed"]
