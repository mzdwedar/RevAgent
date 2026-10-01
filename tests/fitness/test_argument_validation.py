"""Tools, MCP, capability surfaces: a schema validates shape - so it has to actually be used to
validate shape.

`input_schema` was declared on every tool, checked at construction for a "type" key,
and then never consulted again. Every prepare() function was doing ad-hoc coercion on
untrusted model output, so a missing field was a KeyError and a garbage number was a
ValueError, both escaping the turn uncaught: no response span, no audit record, no
reply. That is fail-open on the one input the system is told to distrust.
"""

from __future__ import annotations

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, handle
from agentstack.runtime.run import Run
from agentstack.tools.catalog import build_registry
from agentstack.tools.experiments import ROLLOUT_STAGE
from agentstack.tools.validation import InvalidToolArguments, validate_arguments

from .conftest import ROLLOUT_ARGS, ROLLOUT_MESSAGE, SCOPES, TENANT, USER

REGISTRY = build_registry()
EXPOSED = REGISTRY.expose_for(tenant=TENANT, stage=ROLLOUT_STAGE)
GOOD = ROLLOUT_ARGS
TOOL = "roll_out_variant_to_percentage"


def test_a_missing_required_field_is_a_typed_refusal() -> None:
    with pytest.raises(InvalidToolArguments, match="percentage"):
        REGISTRY.prepare(TOOL, {"tenant": "acme", "experiment_id": "exp-7"}, exposed=EXPOSED)


def test_a_wrongly_typed_field_is_a_typed_refusal() -> None:
    with pytest.raises(InvalidToolArguments, match="percentage"):
        REGISTRY.prepare(TOOL, {**GOOD, "percentage": "quite a lot"}, exposed=EXPOSED)


def test_an_unexpected_field_is_a_typed_refusal() -> None:
    """Extra arguments are how a proposal smuggles something past a narrow tool."""
    with pytest.raises(InvalidToolArguments, match="override_limits"):
        REGISTRY.prepare(TOOL, {**GOOD, "override_limits": True}, exposed=EXPOSED)


def test_well_formed_arguments_still_pass() -> None:
    request = REGISTRY.prepare(TOOL, GOOD, exposed=EXPOSED)
    assert request.payload["percentage"] == 10


def test_the_validator_reads_the_declared_schema_not_a_hand_written_copy() -> None:
    spec = REGISTRY.spec(TOOL)
    validate_arguments(spec, GOOD)
    with pytest.raises(InvalidToolArguments):
        validate_arguments(spec, {})


@pytest.mark.parametrize(
    ("arguments", "complaint"),
    [
        ({"limit": 51}, "at most 50"),
        ({"limit": 0}, "at least 1"),
        ({"limit": "5000"}, "at most 50"),
        ({"status": "approved"}, "must be one of"),
    ],
)
def test_a_declared_bound_is_enforced_not_just_declared(
    arguments: dict[str, object], complaint: str
) -> None:
    """A budget in the schema that nothing checks is a budget in name only: `limit: 5000`
    would reach the surface looking validated."""
    spec = REGISTRY.spec("list_experiments")

    with pytest.raises(InvalidToolArguments, match=complaint):
        validate_arguments(spec, {"tenant": TENANT, **arguments})


def test_a_value_inside_its_bounds_passes() -> None:
    spec = REGISTRY.spec("list_experiments")

    assert validate_arguments(spec, {"tenant": TENANT, "limit": "50", "status": "live"}) == {
        "tenant": TENANT,
        "limit": 50,
        "status": "live",
    }


def test_a_malformed_proposal_fails_closed_and_traceably(stack: Stack, run: Run) -> None:
    """The turn ends with evidence and a reply, not a stack trace."""
    garbled = InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=run.session_id,
        text=ROLLOUT_MESSAGE.replace("percentage=10", "percentage=lots"),
    )
    result = handle(stack, garbled, scopes=SCOPES, run=run)

    assert result.status == "rejected"
    assert "response" in result.tracer.names(), "a refused turn still emits a response span"
    assert "tool.reject" in result.tracer.names()
    assert stack.registry_client.rollouts == []
    assert stack.transcripts.for_session(run.session_id), "the user still gets a reply"
