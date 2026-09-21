"""Part 8: traces explain execution; audit records preserve accountability."""

from __future__ import annotations

import contextlib

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, handle
from agentstack.observability.audit import AuditSink
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import Run

from .conftest import SCOPES, approve_and_resume


def test_they_are_different_sinks(stack: Stack) -> None:
    assert isinstance(stack.audit, AuditSink)
    assert not isinstance(stack.audit, Tracer)
    assert not hasattr(stack.audit, "span"), "an audit sink is not a place to put debug spans"


def test_a_side_effect_produces_an_audit_record_with_the_acting_identity(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    handle(stack, event, scopes=SCOPES, run=run)

    records = stack.audit.for_run(run.run_id)
    assert len(records) == 1
    record = records[0]
    assert record.principal == run.user
    assert record.tenant == run.tenant
    assert record.approval_id, "the audit trail names the approval that authorized the act"
    assert record.policy_decision == "allow"
    assert record.outcome == "committed"
    assert record.resource.startswith(f"{run.tenant}/")


def test_a_denied_action_is_audited_too(stack: Stack, event: InboundEvent, run: Run) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    with contextlib.suppress(PermissionError):
        handle(stack, event, scopes=frozenset({"billing:read"}), run=run)
    outcomes = {r.outcome for r in stack.audit.for_run(run.run_id)}
    assert "denied" in outcomes, "a refusal is evidence and belongs in the audit trail"


def test_a_read_only_call_is_traced_but_not_audited(stack: Stack, run: Run) -> None:
    lookup = InboundEvent(
        channel="test",
        tenant=run.tenant,
        user_id=run.user,
        session_id=run.session_id,
        text="lookup_subscription tenant=acme customer_id=c-42",
    )
    result = handle(stack, lookup, scopes=SCOPES, run=run)
    assert result.status == "complete"
    assert "execution.commit" in result.tracer.names()
    assert stack.audit.for_run(run.run_id) == [], (
        "accountability records are for side effects, not for every read"
    )
