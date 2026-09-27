"""Part 4: the same side effect, requested twice, happens once."""

from __future__ import annotations

import grimp

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


def test_no_key_can_be_derived_from_temporal_identity() -> None:
    """ADR-0008 rule 1, and half of spec criterion 36 (E1).

    A key built from a workflow id, a Temporal run id, an activity id or an attempt
    number lands twice under retry, reset or redelivery. That's what the probe
    measured: only the content-derived key landed once under all three. The keys are
    built in `agentstack.tools`, so the tools have no path to `temporalio` at all,
    directly or through anything they import.
    """
    graph = grimp.build_graph("agentstack", include_external_packages=True)

    assert graph.chain_exists(
        importer="agentstack.runtime", imported="temporalio", as_packages=True
    ), "control: the runtime does reach temporalio, so a False below is a real answer"
    chain = graph.find_shortest_chain(importer="agentstack.tools", imported="temporalio")
    assert not graph.chain_exists(
        importer="agentstack.tools", imported="temporalio", as_packages=True
    ), f"agentstack.tools reaches temporalio: {chain}"
