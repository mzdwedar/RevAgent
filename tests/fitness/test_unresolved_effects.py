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

import threading
from typing import Any

import pytest

from agentstack.execution.gateway import UnresolvedEffect
from agentstack.execution.idempotency import ClaimState, IdempotencyLedger
from agentstack.execution.surfaces import SurfaceRefused
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import Run
from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.catalog import REFUND

from .conftest import SCOPES, TENANT, approve_and_resume


def test_the_ledger_distinguishes_unknown_from_not_yet_done(app_database: Database) -> None:
    ledger = IdempotencyLedger(db=app_database)
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


# --- refused is not unresolved (T24) ---
#
# "The surface refused before acting" and "the answer was lost" look identical from
# inside the gateway only if the surface cannot say which one happened. A guarded write
# that matched no row can: nothing applied. Treating that as unresolved would leave a
# key nobody needs to reconcile, and block the legitimate call that comes after it.


def _refused_turn(stack: Stack, event: InboundEvent, run: Run) -> Any:
    parked = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, parked, run)
    stack.client.refuse_before_effect = True
    return handle(stack, event, scopes=SCOPES, run=run)


def test_a_refused_effect_releases_its_claim(stack: Stack, event: InboundEvent, run: Run) -> None:
    _refused_turn(stack, event, run)

    assert stack.client.calls == [], "a refusal is a surface that did not act"
    assert stack.ledger.unresolved_keys() == (), (
        "a refusal left a claim someone would have to reconcile for nothing"
    )


def test_a_refused_effect_is_audited_as_refused_not_unresolved(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    _refused_turn(stack, event, run)

    outcomes = {(r.outcome, r.policy_decision) for r in stack.audit.for_run(run.run_id)}
    assert ("refused", "surface.refused") in outcomes
    assert not any(outcome == "unresolved" for outcome, _ in outcomes)


def test_the_turn_answers_a_refusal_instead_of_crashing(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    result = _refused_turn(stack, event, run)

    assert result.status == "rejected"
    assert "tool.reject" in result.tracer.names()
    assert "refused" in result.text


def test_after_a_refusal_the_same_effect_can_still_happen_once(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    """The key was released, so a later call - the state has moved on, the precondition
    now holds - acts. It acts once."""
    _refused_turn(stack, event, run)
    stack.client.refuse_before_effect = False

    handle(stack, event, scopes=SCOPES, run=run)
    handle(stack, event, scopes=SCOPES, run=run)

    assert len(stack.client.calls) == 1


def test_the_gateway_reraises_the_refusal_to_its_caller(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    """The runtime turns it into an answer; a caller without a turn still sees it."""
    parked = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, parked, run)
    stack.client.refuse_before_effect = True
    request = parked.pending_request
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)

    with pytest.raises(SurfaceRefused):
        stack.deps.gateway.execute(
            request=request,
            spec=REFUND,
            envelope=envelope_for(view, scopes=SCOPES),
            run_id=run.run_id,
            state_snapshot=parked.pending_wait.state_snapshot,
            tracer=tracer,
        )


def test_an_unresolved_claim_survives_the_process_that_made_it(
    stack: Stack, event: InboundEvent, run: Run, app_database: Database, checkpointer: Any
) -> None:
    """The state the whole two-phase design exists for.

    A ledger that only lives in the process is at its least useful exactly when it is
    most needed: the process that dispatched the effect is the one that died.
    """
    _approved_run(stack, event, run)
    stack.client.fail_after_effect = True
    with pytest.raises(UnresolvedEffect):
        handle(stack, event, scopes=SCOPES, run=run)

    restarted = build_stack(app_database, checkpointer, tenant=TENANT)

    assert restarted.ledger.unresolved_keys() == stack.ledger.unresolved_keys()
    key = restarted.ledger.unresolved_keys()[0]
    assert restarted.ledger.claim(key).state is ClaimState.IN_FLIGHT
    assert restarted.ledger.recorded(key) is None


def test_a_restarted_process_refuses_to_retry_the_unresolved_effect(
    stack: Stack, event: InboundEvent, run: Run, app_database: Database, checkpointer: Any
) -> None:
    """A fresh process is the most dangerous retrier: it remembers nothing."""
    _approved_run(stack, event, run)
    stack.client.fail_after_effect = True
    with pytest.raises(UnresolvedEffect):
        handle(stack, event, scopes=SCOPES, run=run)

    restarted = build_stack(app_database, checkpointer, tenant=TENANT)
    with pytest.raises(UnresolvedEffect, match="(?i)reconcile"):
        handle(restarted, event, scopes=SCOPES, run=run)

    assert restarted.client.calls == [], "the money left twice across a restart"


def test_two_processes_claiming_one_key_produce_one_winner(app_database: Database) -> None:
    """Read-then-insert lets both callers see an empty ledger and both commit.

    `ON CONFLICT DO NOTHING RETURNING` is what decides it: exactly one gets a row.
    """
    ledger = IdempotencyLedger(db=app_database)
    states: list[ClaimState] = []
    barrier = threading.Barrier(4)

    def attempt() -> None:
        barrier.wait()
        states.append(ledger.claim("refund:acme:ch-contended:1999").state)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert states.count(ClaimState.FRESH) == 1, f"{states.count(ClaimState.FRESH)} callers may act"
    assert states.count(ClaimState.IN_FLIGHT) == 3


def test_a_settled_claim_cannot_be_half_recorded(app_database: Database) -> None:
    """A receipt with no time, or a time with no receipt, is an outcome nobody can read."""
    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(
            "INSERT INTO idempotency_claims (key, claimed_at, receipt) VALUES (%s, now(), 'r')",
            ("half-recorded",),
        )

    assert caught.value.constraint == "settled_claims_record_both"
