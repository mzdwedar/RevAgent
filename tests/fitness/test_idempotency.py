"""Part 4: the same side effect, requested twice, happens once."""

from __future__ import annotations

from itertools import permutations
from typing import Any

import pytest

from agentstack.execution.gateway import Gateway
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import Run
from agentstack.tools.catalog import REFUND, build_registry
from agentstack.tools.spec import ToolSpec

from .conftest import SCOPES, approve_and_resume


def test_a_retry_does_not_refund_twice(stack: Stack, event: InboundEvent, run: Run) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)

    handle(stack, event, scopes=SCOPES, run=run)
    handle(stack, event, scopes=SCOPES, run=run)
    handle(stack, event, scopes=SCOPES, run=run)

    assert len(stack.client.calls) == 1, (
        f"the surface was hit {len(stack.client.calls)} times; a recorded step boundary "
        "plus an idempotency key is what stops a retry from duplicating a committed effect"
    )


def test_the_gateway_deduplicates_even_without_a_step_boundary(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    """Belt and braces: the ledger is the second line of defence behind the step."""
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    request = first.pending_request
    assert request is not None

    gateway: Gateway = stack.deps.gateway
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    envelope = envelope_for(view, scopes=SCOPES)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)
    assert first.pending_wait is not None
    snapshot = first.pending_wait.state_snapshot

    a = gateway.execute(
        request=request,
        spec=REFUND,
        envelope=envelope,
        run_id=run.run_id,
        state_snapshot=snapshot,
        tracer=tracer,
    )
    b = gateway.execute(
        request=request,
        spec=REFUND,
        envelope=envelope,
        run_id=run.run_id,
        state_snapshot=snapshot,
        tracer=tracer,
    )
    assert a.receipt == b.receipt
    assert b.deduplicated is True
    assert len(stack.client.calls) == 1


def test_the_idempotency_key_is_the_business_identity_of_the_effect() -> None:
    from agentstack.tools.catalog import _refund

    args = {"tenant": "acme", "customer_id": "c-1", "charge_id": "ch-9", "amount_cents": 500}
    assert _refund(args).idempotency_key == _refund(dict(args)).idempotency_key, (
        "a retry must reproduce the key, so it cannot be a random uuid"
    )


# --- the key identifies one effect ---

# Two ways to split one string across two ids. `:` is a legal id character, so a key
# that joins its parts with `:` cannot tell `a` + `b:c` from `a:b` + `c` - and the
# ledger answers the second effect with the first one's receipt, before the surface
# is ever called. Found by the registry stack audit: experiment `exp-7` at version
# `exp:H` and experiment `exp-7:exp` at version `H` shared a halt key.
_SPLITS = (("a", "b:c"), ("a:b", "c"))
_REGISTRY = build_registry()


def _baseline(spec: ToolSpec) -> dict[str, Any]:
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


@pytest.mark.parametrize("spec", _REGISTRY.specs(), ids=lambda s: s.name)
def test_two_valid_requests_for_different_effects_never_share_a_key(spec: ToolSpec) -> None:
    """Held over the whole catalog: a key is the effect's identity, so it must be
    injective over every argument set validation lets through."""
    ids = [n for n, d in spec.input_schema["properties"].items() if d.get("format") == "id"]
    for first, second in permutations(ids, 2):
        requests = [
            _REGISTRY.prepare(
                spec.name,
                {**_baseline(spec), first: x, second: y},
                exposed=_REGISTRY.specs(),
            )
            for x, y in _SPLITS
        ]
        left, right = requests
        if (left.resource, left.payload) != (right.resource, right.payload):
            assert left.idempotency_key != right.idempotency_key, (
                f"{spec.name}: {first}/{second} split differently name different effects "
                f"({left.resource} vs {right.resource}) under one key {left.idempotency_key!r}"
            )
