"""Part 8: traces explain execution; audit records preserve accountability."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.observability.audit import AuditSink
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import Run
from agentstack.storage.database import Database

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

    committed = [r for r in stack.audit.for_run(run.run_id) if r.outcome == "committed"]
    assert len(committed) == 1
    record = committed[0]
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
    assert "execution.read" in result.tracer.names()
    assert "execution.commit" not in result.tracer.names(), "a read is not a commit"
    assert stack.audit.for_run(run.run_id) == [], (
        "accountability records are for side effects, not for every read"
    )


def test_a_refused_approval_is_audited(stack: Stack, event: InboundEvent, run: Run) -> None:
    """The run reached the choke point and was stopped. That is an accountability event."""
    handle(stack, event, scopes=SCOPES, run=run)
    refusals = [r for r in stack.audit.for_run(run.run_id) if r.outcome == "refused"]
    assert refusals, "an action stopped for want of approval left no audit record"
    assert refusals[0].policy_decision == "approval.required"
    assert refusals[0].approval_id is None


def test_a_containment_violation_is_audited(stack: Stack, run: Run) -> None:
    """A human approved it and containment stopped it anyway - the most interesting
    event the system can produce, and it used to leave nothing behind."""
    from agentstack.execution.surfaces import SandboxViolation
    from agentstack.interfaces.wiring import envelope_for
    from agentstack.observability.spans import Tracer
    from agentstack.tools.action import ActionRequest
    from agentstack.tools.catalog import REFUND
    from agentstack.tools.spec import Surface

    outside = ActionRequest(
        tool=REFUND.name,
        surface=Surface.API,
        resource=f"{run.tenant}/invoices/i-1",
        payload={"amount_cents": 1},
        idempotency_key="k-contained",
    )
    stack.approvals.grant(
        run_id=run.run_id,
        request=outside,
        state_snapshot="s",
        approver="finance-oncall",
        summary="refund 0.01 on invoice i-1",
    )
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)
    with pytest.raises(SandboxViolation):
        stack.deps.gateway.execute(
            request=outside,
            spec=REFUND,
            envelope=envelope_for(view, scopes=SCOPES),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=tracer,
        )
    contained = [r for r in stack.audit.for_run(run.run_id) if r.outcome == "contained"]
    assert contained, "containment stopped an approved action and recorded nothing"
    assert contained[0].policy_decision == "containment.violation"
    assert contained[0].approval_id, "the audit trail names the approval it overrode"


def test_the_audit_trail_is_not_in_the_same_schema_as_the_operational_tables(
    app_database: Database,
) -> None:
    """Separate retention and access rules need somewhere to attach.

    `audit.records`, not `public.audit_records`: granting read on the operational
    schema should not hand over the accountability trail with it.
    """
    rows = app_database.fetch_all("SELECT schemaname FROM pg_tables WHERE tablename = 'records'")

    assert [row[0] for row in rows] == ["audit"]


def test_deleting_the_run_does_not_delete_what_it_was_accountable_for(
    stack: Stack, event: InboundEvent, run: Run, app_database: Database
) -> None:
    """Every other table cascades from the session. This one must not.

    "We deleted the session" is not an answer to "who authorised this refund". An
    audit trail that a retention job can erase as a side effect of tidying up is a
    debug log with a longer TTL, which is the Part 8 boundary collapsing.
    """
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    handle(stack, event, scopes=SCOPES, run=run)
    before = stack.audit.for_run(run.run_id)
    assert before, "expected the committed refund to be audited"

    app_database.execute("DELETE FROM sessions WHERE session_id = %s", (run.session_id,))

    assert app_database.fetch_all("SELECT run_id FROM runs WHERE run_id = %s", (run.run_id,)) == []
    assert stack.audit.for_run(run.run_id) == before


def test_audit_records_outlive_the_process_that_wrote_them(
    stack: Stack, event: InboundEvent, run: Run, app_database: Database, checkpointer: Any
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    handle(stack, event, scopes=SCOPES, run=run)

    restarted = build_stack(app_database, checkpointer, tenant=run.tenant)

    assert restarted.audit.for_run(run.run_id) == stack.audit.for_run(run.run_id)
