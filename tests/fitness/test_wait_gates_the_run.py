"""Runtime, workflows, durable execution: satisfying the wait is a precondition of continuing, not a
parallel ritual.

`run_turn` parked a wait and then never read `deps.waits` on the way in, and `handle`
just called `run_turn` again. In every driver - the CLI, the eval runner, the test
conftest - `resume(...)` and the follow-up `handle(...)` were two independent
statements, and deleting the `resume` call from either left the rollout committing
anyway, because the only thing the continuation actually consulted was the
ApprovalStore.

That made `test_waiting_is_state.py` true of the store and false of the system: a
resume event that is *rejected* - the wait was already satisfied, or the state moved -
did not stop the next turn on that run from reaching a surface.

This is the test the audit asked for: a run with a pending, unsatisfied wait cannot
reach a surface.
"""

from __future__ import annotations

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, handle
from agentstack.runtime.run import Run
from agentstack.runtime.waits import ResumeEvent, ResumeRejected, resume

from .conftest import SCOPES


def test_an_approved_run_with_an_unsatisfied_wait_still_cannot_commit(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    assert first.status == "awaiting_approval"
    assert first.pending_wait is not None and first.pending_request is not None

    # Approve, but never satisfy the wait.
    stack.approvals.grant(
        run_id=run.run_id,
        request=first.pending_request,
        state_snapshot=first.pending_wait.state_snapshot,
        approver="finance-oncall",
        summary=first.approval_summary or "roll out",
    )

    second = handle(stack, event, scopes=SCOPES, run=run)
    assert second.status == "blocked"
    assert stack.registry_client.rollouts == [], (
        "the approval store alone let the run continue; the wait was decorative"
    )
    assert stack.waits.pending_for(run.run_id), "the wait is still pending, as it should be"


def test_a_rejected_resume_leaves_the_run_blocked(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    assert first.pending_wait is not None and first.pending_request is not None
    stack.approvals.grant(
        run_id=run.run_id,
        request=first.pending_request,
        state_snapshot=first.pending_wait.state_snapshot,
        approver="finance-oncall",
        summary=first.approval_summary or "roll out",
    )

    with pytest.raises(ResumeRejected):
        resume(
            stack.waits,
            ResumeEvent(
                run_id=run.run_id,
                wait_id=first.pending_wait.wait_id,
                state_snapshot="the-world-moved",
                payload={},
            ),
        )

    assert handle(stack, event, scopes=SCOPES, run=run).status == "blocked"
    assert stack.registry_client.rollouts == []


def test_a_satisfied_wait_lets_the_run_through_and_carries_its_approver(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    assert first.pending_wait is not None and first.pending_request is not None
    stack.approvals.grant(
        run_id=run.run_id,
        request=first.pending_request,
        state_snapshot=first.pending_wait.state_snapshot,
        approver="finance-oncall",
        summary=first.approval_summary or "roll out",
    )
    resume(
        stack.waits,
        ResumeEvent(
            run_id=run.run_id,
            wait_id=first.pending_wait.wait_id,
            state_snapshot=first.pending_wait.state_snapshot,
            payload={"approved_by": "finance-oncall"},
        ),
    )

    second = handle(stack, event, scopes=SCOPES, run=run)
    assert second.status == "complete"
    assert len(stack.registry_client.rollouts) == 1

    start = next(span for span in second.tracer.spans if span.name == "run.start")
    assert start.attributes.get("resumed_by") == "finance-oncall", (
        "the evidence should show what let this run continue, not just that it did"
    )
