"""Shared builders for the architecture fitness suite.

These tests assert the invariants in STACK.md. They are not unit tests of behavior:
each one fails when a layer boundary has been collapsed, which is the only thing that
makes the boundary real.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.runtime.run import Run, new_run
from agentstack.storage.database import Database

TENANT = "acme"
USER = "agent-operator"
SCOPES = frozenset({"billing:read", "billing:refund"})
REFUND_MESSAGE = "issue_refund tenant=acme customer_id=c-42 charge_id=ch-7 amount_cents=1999"


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Iterator[Stack]:
    yield build_stack(app_database, checkpointer, tenant=TENANT)


@pytest.fixture
def session_id(stack: Stack) -> str:
    return stack.resolver.start(user_id=USER, tenant=TENANT).session_id


@pytest.fixture
def event(session_id: str) -> InboundEvent:
    return InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=session_id,
        text=REFUND_MESSAGE,
    )


@pytest.fixture
def run(stack: Stack, session_id: str) -> Run:
    """A persisted run. Steps and waits carry a foreign key onto it."""
    return stack.runs.ensure(
        new_run(session_id=session_id, tenant=TENANT, user=USER, channel="test")
    )


def approve_and_resume(stack: Stack, result, run: Run, approver: str = "finance-oncall") -> None:
    """Grant the approval the run is parked on, then satisfy the wait.

    Deliberately explicit: the approval binds to this run, this action fingerprint and
    the state snapshot the approver was shown.
    """
    from agentstack.runtime.waits import ResumeEvent, resume

    wait = result.pending_wait
    request = result.pending_request
    assert wait is not None and request is not None, "expected the run to be awaiting approval"
    assert result.approval_summary, "an awaiting run must carry what the approver will see"
    stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot=wait.state_snapshot,
        approver=approver,
        summary=result.approval_summary,
    )
    resume(
        stack.waits,
        ResumeEvent(
            run_id=run.run_id,
            wait_id=wait.wait_id,
            state_snapshot=wait.state_snapshot,
            payload={"approved_by": approver},
        ),
    )


def drive_to_completion(
    stack: Stack,
    event: InboundEvent,
    run: Run,
    *,
    scopes: frozenset[str] = SCOPES,
    approver: str = "finance-oncall",
    max_rounds: int = 6,
):
    """Run the turn, approving whatever it parks on, until it stops parking.

    This is what an approval queue looks like from the outside: each action surfaces
    on its own, bound to its own state, and is approved on its own. A helper that
    approved everything up front would be the modal this whole design rejects.
    """
    result = handle(stack, event, scopes=scopes, run=run)
    for _ in range(max_rounds):
        if result.status != "awaiting_approval":
            return result
        approve_and_resume(stack, result, run, approver)
        result = handle(stack, event, scopes=scopes, run=run)
    raise AssertionError(f"still parked after {max_rounds} rounds")
