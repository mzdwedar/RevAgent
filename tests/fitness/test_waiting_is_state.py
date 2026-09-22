"""Part 4: waiting is persisted state, and a resume event has to prove it belongs.

Since T3 the waits are rows. That is what makes the word "persisted" checkable: a
wait now outlives the object that parked it, and a second resume racing the first is
settled by the database rather than by whichever caller read the row last.
"""

from __future__ import annotations

import threading

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.runtime.run import Run
from agentstack.runtime.waits import ResumeEvent, ResumeRejected, resume
from agentstack.storage.database import Database, IntegrityViolation

from .conftest import SCOPES, TENANT


def test_a_paused_run_leaves_a_persisted_wait(stack: Stack, event: InboundEvent, run: Run) -> None:
    result = handle(stack, event, scopes=SCOPES, run=run)
    assert result.status == "awaiting_approval"
    pending = stack.waits.pending_for(run.run_id)
    assert len(pending) == 1
    assert pending[0].kind == "human_approval"
    assert pending[0].state_snapshot, "a wait without a state snapshot resumes into the dark"


def test_a_resume_event_must_identify_run_wait_and_state(stack: Stack, run: Run) -> None:
    wait = stack.waits.park(run_id=run.run_id, kind="human_approval", state_snapshot="fp-1")

    with pytest.raises(ResumeRejected, match="different run"):
        resume(stack.waits, ResumeEvent("run-other", wait.wait_id, "fp-1", {}))
    with pytest.raises(ResumeRejected, match="not a known wait"):
        resume(stack.waits, ResumeEvent(run.run_id, "wait-nope", "fp-1", {}))
    with pytest.raises(ResumeRejected, match="state this run was paused against has changed"):
        resume(stack.waits, ResumeEvent(run.run_id, wait.wait_id, "fp-2", {}))

    satisfied = resume(stack.waits, ResumeEvent(run.run_id, wait.wait_id, "fp-1", {"ok": True}))
    assert satisfied.satisfied is True
    assert stack.waits.pending_for(run.run_id) == ()


def test_a_wait_cannot_be_satisfied_twice(stack: Stack, run: Run) -> None:
    wait = stack.waits.park(run_id=run.run_id, kind="webhook", state_snapshot="fp-1")
    resume(stack.waits, ResumeEvent(run.run_id, wait.wait_id, "fp-1", {}))
    with pytest.raises(ResumeRejected, match="already satisfied"):
        resume(stack.waits, ResumeEvent(run.run_id, wait.wait_id, "fp-1", {}))


def test_a_wait_outlives_the_process_that_parked_it(
    stack: Stack, run: Run, app_database: Database
) -> None:
    """The property the word "persisted" was claiming before T3."""
    wait = stack.waits.park(run_id=run.run_id, kind="human_approval", state_snapshot="fp-1")

    restarted = build_stack(app_database, tenant=TENANT)
    pending = restarted.waits.pending_for(run.run_id)

    assert [w.wait_id for w in pending] == [wait.wait_id]
    assert pending[0].state_snapshot == "fp-1"
    assert pending[0].created_at == wait.created_at


def test_what_satisfied_the_wait_is_readable_afterwards(
    stack: Stack, run: Run, app_database: Database
) -> None:
    """The resume payload is the audit trail of why the run continued."""
    wait = stack.waits.park(run_id=run.run_id, kind="human_approval", state_snapshot="fp-1")
    resume(
        stack.waits,
        ResumeEvent(run.run_id, wait.wait_id, "fp-1", {"approved_by": "finance-oncall"}),
    )

    restarted = build_stack(app_database, tenant=TENANT)
    satisfied = restarted.waits.satisfied_for(run.run_id)

    assert [w.payload["approved_by"] for w in satisfied] == ["finance-oncall"]


def test_a_wait_for_a_run_that_does_not_exist_is_refused(stack: Stack) -> None:
    """An orphan wait is something T10 would later try to resume into nothing."""
    with pytest.raises(IntegrityViolation) as caught:
        stack.waits.park(run_id="run-never-created", kind="webhook", state_snapshot="fp-1")

    assert caught.value.constraint == "waits_run_id_fkey"


def test_two_resumes_arriving_together_produce_one_winner(stack: Stack, run: Run) -> None:
    """Two approvers clicking at once. The read-then-decide checks cannot settle this.

    Without `AND NOT satisfied` in the UPDATE both callers pass every check in
    `resume` and both are told they resumed the run, which is two different people
    each believing they are the reason it continued.
    """
    wait = stack.waits.park(run_id=run.run_id, kind="human_approval", state_snapshot="fp-1")
    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def attempt(approver: str) -> None:
        barrier.wait()
        try:
            outcomes.append(
                resume(
                    stack.waits,
                    ResumeEvent(run.run_id, wait.wait_id, "fp-1", {"approved_by": approver}),
                )
            )
        except ResumeRejected as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt, args=(name,)) for name in ("ana", "ben")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    winners = [o for o in outcomes if not isinstance(o, ResumeRejected)]
    assert len(winners) == 1, f"{len(winners)} callers were told they resumed the run"
    assert len(stack.waits.satisfied_for(run.run_id)) == 1
