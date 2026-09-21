"""Part 4: the same side effect, requested twice, happens once."""

from __future__ import annotations

from agentstack.execution.gateway import Gateway
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import Run
from agentstack.tools.catalog import REFUND

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
