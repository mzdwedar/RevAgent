"""Part 4: the ledger has to know about intent, not only about success.

`record()` ran after `commit()` returned, so the ledger only ever knew about effects
that had already come back. `RecordingClient` never fails, which is why every
idempotency test passed anyway.

Replace it with an HTTP client and the first socket timeout on a refund the provider
did apply leaves no ledger row: the step ledger writes `failed`, the run retries,
`recorded()` returns None, and the money leaves twice. The ledger cannot prevent that
while its only state is "already returned".

Two phases, and a third state: claim the key before committing, finalize it with the
receipt after, and treat a claimed-but-unfinalized key as **unknown outcome** -
never as "not yet done".
"""

from __future__ import annotations

import pytest

from agentstack.execution.gateway import UnresolvedEffect
from agentstack.execution.idempotency import ClaimState, IdempotencyLedger
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import Run
from agentstack.tools.catalog import REFUND

from .conftest import SCOPES, approve_and_resume


def test_the_ledger_distinguishes_unknown_from_not_yet_done() -> None:
    ledger = IdempotencyLedger()
    assert ledger.claim("k").state is ClaimState.FRESH
    assert ledger.claim("k").state is ClaimState.IN_FLIGHT, (
        "a claimed key whose outcome never came back is unknown, not available"
    )
    ledger.finalize("k", "receipt-1")
    settled = ledger.claim("k")
    assert settled.state is ClaimState.COMMITTED
    assert settled.receipt == "receipt-1"


def _approved_run(stack: Stack, event: InboundEvent, run: Run) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)


def test_an_effect_that_applied_but_never_answered_is_not_repeated(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    _approved_run(stack, event, run)
    stack.client.fail_after_effect = True

    with pytest.raises(UnresolvedEffect):
        handle(stack, event, scopes=SCOPES, run=run)
    assert len(stack.client.calls) == 1, "the effect applied once"

    # The retry is the dangerous moment. It must refuse, not re-commit.
    stack.client.fail_after_effect = False
    with pytest.raises(UnresolvedEffect, match="(?i)reconcile"):
        handle(stack, event, scopes=SCOPES, run=run)
    assert len(stack.client.calls) == 1, "the money left twice"


def test_an_unresolved_effect_is_audited_so_someone_can_reconcile_it(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    _approved_run(stack, event, run)
    stack.client.fail_after_effect = True
    with pytest.raises(UnresolvedEffect):
        handle(stack, event, scopes=SCOPES, run=run)

    unresolved = [r for r in stack.audit.for_run(run.run_id) if r.outcome == "unresolved"]
    assert unresolved, "an effect of unknown outcome is the record someone needs most"
    assert unresolved[0].action_fingerprint


def test_reconciliation_settles_the_key_and_the_retry_deduplicates(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    _approved_run(stack, event, run)
    stack.client.fail_after_effect = True
    with pytest.raises(UnresolvedEffect):
        handle(stack, event, scopes=SCOPES, run=run)

    # What a reconciliation job does after asking the surface what really happened.
    key = next(iter(stack.ledger.unresolved_keys()))
    stack.ledger.finalize(key, "receipt-reconciled")

    stack.client.fail_after_effect = False
    exposed = stack.deps.registry.expose_for(tenant=run.tenant)
    request = stack.deps.registry.prepare(
        "issue_refund",
        {"tenant": "acme", "customer_id": "c-42", "charge_id": "ch-7", "amount_cents": 1999},
        exposed=exposed,
    )
    stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot="s",
        approver="finance-oncall",
        summary="retry after reconciliation",
    )
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)
    result = stack.deps.gateway.execute(
        request=request,
        spec=REFUND,
        envelope=envelope_for(view, scopes=SCOPES),
        run_id=run.run_id,
        state_snapshot="s",
        tracer=tracer,
    )
    assert result.deduplicated is True
    assert result.receipt == "receipt-reconciled"
    assert len(stack.client.calls) == 1
