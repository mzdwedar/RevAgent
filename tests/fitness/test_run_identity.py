"""Runtime, workflows, durable execution: one stable id ties input, state, tool calls, waits,
approvals and traces."""

from __future__ import annotations

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, handle
from agentstack.runtime.run import Run

from .conftest import SCOPES, approve_and_resume, fresh_run


def test_every_span_carries_the_run_and_session_id(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    result = handle(stack, event, scopes=SCOPES, run=run)
    assert result.tracer.spans
    for span in result.tracer.spans:
        assert span.run_id == run.run_id
        assert span.session_id == run.session_id


def test_the_run_id_survives_a_wait_and_a_resume(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    assert first.status == "awaiting_approval"
    assert first.pending_wait is not None
    assert first.pending_wait.run_id == run.run_id

    approve_and_resume(stack, first, run)
    second = handle(stack, event, scopes=SCOPES, run=run)

    assert second.status == "complete"
    assert second.run_id == first.run_id
    assert stack.audit.for_run(run.run_id), "the audit trail hangs off the run id"


def test_an_approval_does_not_carry_across_runs(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)

    other = handle(stack, event, scopes=SCOPES, run=fresh_run(stack, event.session_id))
    assert other.status == "awaiting_approval", (
        "an approval granted in one run must not authorize another run"
    )
