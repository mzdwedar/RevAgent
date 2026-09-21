"""Part 7: approval sits at the side-effect boundary and binds to a specific action."""

from __future__ import annotations

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.policy.approval import ApprovalRequired, ApprovalStale, require_approval
from agentstack.runtime.run import Run
from agentstack.tools.catalog import LOOKUP, REFUND, _refund
from agentstack.tools.spec import Approval

from .conftest import SCOPES, approve_and_resume

ARGS = {"tenant": "acme", "customer_id": "c-42", "charge_id": "ch-7", "amount_cents": 1999}


def test_an_ungated_action_never_reaches_the_surface(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    handle(stack, event, scopes=SCOPES, run=run)
    assert stack.client.calls == [], "no approval, no side effect"


def test_approval_at_task_start_does_not_authorize_a_later_act(stack: Stack) -> None:
    """The classic failure: 'can I complete this task?' asked ten steps too early."""
    vague = _refund({**ARGS, "amount_cents": 1})
    actual = _refund(ARGS)
    stack.approvals.grant(
        run_id="run-1",
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
            run_id="run-1",
            state_snapshot="fp",
        )


def test_a_stale_approval_is_refused(stack: Stack) -> None:
    request = _refund(ARGS)
    stack.approvals.grant(
        run_id="run-1",
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
            run_id="run-1",
            state_snapshot="the-world-moved",
        )


def test_a_read_only_tool_needs_no_approval(stack: Stack) -> None:
    assert LOOKUP.approval is Approval.NONE
    assert (
        require_approval(
            store=stack.approvals,
            spec=LOOKUP,
            request=_refund(ARGS),
            run_id="run-1",
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
    assert "irreversible" in summary
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
