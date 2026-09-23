"""Part 7: approval sits at the side-effect boundary and binds to a specific action."""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.policy.approval import ApprovalRequired, ApprovalStale, require_approval
from agentstack.runtime.run import Run
from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.catalog import LOOKUP, REFUND, _refund
from agentstack.tools.spec import Approval

from .conftest import SCOPES, TENANT, approve_and_resume

ARGS = {"tenant": "acme", "customer_id": "c-42", "charge_id": "ch-7", "amount_cents": 1999}


def test_an_ungated_action_never_reaches_the_surface(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    handle(stack, event, scopes=SCOPES, run=run)
    assert stack.client.calls == [], "no approval, no side effect"


def test_approval_at_task_start_does_not_authorize_a_later_act(stack: Stack, run: Run) -> None:
    """The classic failure: 'can I complete this task?' asked ten steps too early."""
    vague = _refund({**ARGS, "amount_cents": 1})
    actual = _refund(ARGS)
    stack.approvals.grant(
        run_id=run.run_id,
        request=vague,
        state_snapshot="fp",
        approver="someone",
        summary="proceed with the task?",
    )
    with pytest.raises(ApprovalRequired):
        require_approval(
            store=stack.approvals,
            spec=REFUND,
            request=actual,
            run_id=run.run_id,
            state_snapshot="fp",
        )


def test_a_stale_approval_is_refused(stack: Stack, run: Run) -> None:
    request = _refund(ARGS)
    stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot="state-as-shown",
        approver="someone",
        summary="refund 19.99",
    )
    with pytest.raises(ApprovalStale, match="re-ask"):
        require_approval(
            store=stack.approvals,
            spec=REFUND,
            request=request,
            run_id=run.run_id,
            state_snapshot="the-world-moved",
        )


def test_a_read_only_tool_needs_no_approval(stack: Stack, run: Run) -> None:
    assert LOOKUP.approval is Approval.NONE
    assert (
        require_approval(
            store=stack.approvals,
            spec=LOOKUP,
            request=_refund(ARGS),
            run_id=run.run_id,
            state_snapshot="x",
        )
        is None
    )


def test_the_approval_prompt_names_the_action_not_the_task(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    result = handle(stack, event, scopes=SCOPES, run=run)
    summary = result.approval_summary or ""
    assert "issue_refund" in summary
    assert "acme/customers/c-42/charges/ch-7" in summary
    assert "irreversible" in summary.lower()
    assert "agent-operator" in summary, "an approver must see which identity will act"


def test_containment_still_applies_after_approval(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    """Approval is a decision point; the sandbox is a blast-radius limit. Both, not either."""
    from agentstack.execution.surfaces import SandboxViolation
    from agentstack.tools.action import ActionRequest
    from agentstack.tools.spec import Surface

    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)

    outside = ActionRequest(
        tool=REFUND.name,
        surface=Surface.API,
        resource="globex/customers/c-1/charges/ch-1",
        payload={"amount_cents": 1},
        idempotency_key="k",
    )
    stack.approvals.grant(
        run_id=run.run_id,
        request=outside,
        state_snapshot="s",
        approver="someone",
        summary="approved anyway",
    )
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)
    with pytest.raises((SandboxViolation, Exception)) as excinfo:
        stack.deps.gateway.execute(
            request=outside,
            spec=REFUND,
            envelope=envelope_for(view, scopes=SCOPES),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=tracer,
        )
    assert "tenant" in str(excinfo.value).lower()


def test_an_approval_with_nothing_shown_to_the_approver_is_refused(stack: Stack, run: Run) -> None:
    """An empty summary records that a human agreed to a blank screen."""
    with pytest.raises(ValueError, match="summary"):
        stack.approvals.grant(
            run_id=run.run_id,
            request=_refund(ARGS),
            state_snapshot="s",
            approver="someone",
            summary="",
        )
    with pytest.raises(ValueError, match="approver"):
        stack.approvals.grant(
            run_id=run.run_id,
            request=_refund(ARGS),
            state_snapshot="s",
            approver="",
            summary="refund 19.99",
        )


def test_an_approval_outlives_the_process_that_recorded_it(
    stack: Stack, run: Run, app_database: Database, checkpointer: Any
) -> None:
    """An approval that dies with the process is a human asked twice for one decision."""
    request = _refund(ARGS)
    granted = stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot="state-as-shown",
        approver="finance-oncall",
        summary="refund 19.99 on ch-7",
    )

    restarted = build_stack(app_database, checkpointer, tenant=TENANT)
    found = restarted.approvals.find(
        run_id=run.run_id,
        action_fingerprint=request.fingerprint(),
        state_snapshot="state-as-shown",
    )

    assert found == granted
    assert found is not None and found.approver == "finance-oncall"


def test_the_approval_matching_this_state_wins_over_a_later_one(stack: Stack, run: Run) -> None:
    """Approved twice for one action. Reporting the non-matching one as stale is a
    false alarm, and false alarms are how people learn to click through real ones."""
    request = _refund(ARGS)
    matching = stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot="state-a",
        approver="ana",
        summary="refund 19.99",
    )
    stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot="state-b",
        approver="ben",
        summary="refund 19.99",
    )

    found = stack.approvals.find(
        run_id=run.run_id, action_fingerprint=request.fingerprint(), state_snapshot="state-a"
    )

    assert found == matching


def test_without_a_state_snapshot_the_most_recent_approval_is_returned(
    stack: Stack, run: Run
) -> None:
    """Ordered by sequence, not by clock: two grants can share a microsecond."""
    request = _refund(ARGS)
    for approver in ("ana", "ben", "cai"):
        latest = stack.approvals.grant(
            run_id=run.run_id,
            request=request,
            state_snapshot=f"state-{approver}",
            approver=approver,
            summary="refund 19.99",
        )

    found = stack.approvals.find(run_id=run.run_id, action_fingerprint=request.fingerprint())

    assert found == latest


def test_the_database_refuses_a_blank_approver_or_summary(run: Run, app_database: Database) -> None:
    """`grant` checks both. These are worth holding twice."""
    for column, value, constraint in (
        ("approver", "   ", "approval_names_an_approver"),
        ("summary", "", "approval_records_what_was_shown"),
    ):
        values = {"approver": "someone", "summary": "refund 19.99"}
        values[column] = value
        with pytest.raises(IntegrityViolation) as caught:
            app_database.execute(
                "INSERT INTO approvals (id, run_id, action_fingerprint, state_snapshot,"
                " approver, granted_at, summary) VALUES (%s, %s, 'fp', 's', %s, now(), %s)",
                (f"a-{column}", run.run_id, values["approver"], values["summary"]),
            )
        assert caught.value.constraint == constraint


def test_an_approval_for_a_run_that_does_not_exist_is_refused(stack: Stack) -> None:
    with pytest.raises(IntegrityViolation) as caught:
        stack.approvals.grant(
            run_id="run-never-created",
            request=_refund(ARGS),
            state_snapshot="s",
            approver="someone",
            summary="refund 19.99",
        )

    assert caught.value.constraint == "approvals_run_id_fkey"
