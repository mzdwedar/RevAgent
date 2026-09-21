"""Part 4: waiting is persisted state, and a resume event has to prove it belongs."""

from __future__ import annotations

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, handle
from agentstack.runtime.run import Run
from agentstack.runtime.waits import ResumeEvent, ResumeRejected, WaitStore, resume

from .conftest import SCOPES


def test_a_paused_run_leaves_a_persisted_wait(stack: Stack, event: InboundEvent, run: Run) -> None:
    result = handle(stack, event, scopes=SCOPES, run=run)
    assert result.status == "awaiting_approval"
    pending = stack.waits.pending_for(run.run_id)
    assert len(pending) == 1
    assert pending[0].kind == "human_approval"
    assert pending[0].state_snapshot, "a wait without a state snapshot resumes into the dark"


def test_a_resume_event_must_identify_run_wait_and_state() -> None:
    store = WaitStore()
    wait = store.park(run_id="run-1", kind="human_approval", state_snapshot="fp-1")

    with pytest.raises(ResumeRejected, match="different run"):
        resume(store, ResumeEvent("run-2", wait.wait_id, "fp-1", {}))
    with pytest.raises(ResumeRejected, match="not a known wait"):
        resume(store, ResumeEvent("run-1", "wait-nope", "fp-1", {}))
    with pytest.raises(ResumeRejected, match="state this run was paused against has changed"):
        resume(store, ResumeEvent("run-1", wait.wait_id, "fp-2", {}))

    satisfied = resume(store, ResumeEvent("run-1", wait.wait_id, "fp-1", {"ok": True}))
    assert satisfied.satisfied is True
    assert store.pending_for("run-1") == ()


def test_a_wait_cannot_be_satisfied_twice() -> None:
    store = WaitStore()
    wait = store.park(run_id="run-1", kind="webhook", state_snapshot="fp-1")
    resume(store, ResumeEvent("run-1", wait.wait_id, "fp-1", {}))
    with pytest.raises(ResumeRejected, match="already satisfied"):
        resume(store, ResumeEvent("run-1", wait.wait_id, "fp-1", {}))
