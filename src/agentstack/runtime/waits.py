"""Waiting is state, not sleep (Part 4).

Human approval, a webhook, a timer, a CI run, a long model job - each pauses a run
that must survive a process restart. A wait is therefore persisted, and a resume event
has to identify the run, the pending wait, the state it was paused against, and the
input that satisfies it. Anything less resumes into a world it never saw.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class ResumeRejected(RuntimeError):
    """The resume event does not match a pending wait, or the world moved."""


@dataclass(frozen=True, slots=True)
class Wait:
    wait_id: str
    run_id: str
    kind: str
    state_snapshot: str
    created_at: datetime
    satisfied: bool = False
    # What the resume event carried. This is how the approver reaches the turn that
    # continues: through the wait that was satisfied, not out of a store the runtime
    # could have consulted without anyone resuming anything.
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResumeEvent:
    run_id: str
    wait_id: str
    state_snapshot: str
    payload: dict[str, Any]


@dataclass(slots=True)
class WaitStore:
    _waits: dict[str, Wait] = field(default_factory=dict)

    def park(self, *, run_id: str, kind: str, state_snapshot: str) -> Wait:
        wait = Wait(
            wait_id=f"wait-{uuid.uuid4()}",
            run_id=run_id,
            kind=kind,
            state_snapshot=state_snapshot,
            created_at=datetime.now(UTC),
        )
        self._waits[wait.wait_id] = wait
        return wait

    def get(self, wait_id: str) -> Wait | None:
        return self._waits.get(wait_id)

    def pending_for(self, run_id: str) -> tuple[Wait, ...]:
        return tuple(w for w in self._waits.values() if w.run_id == run_id and not w.satisfied)

    def satisfied_for(self, run_id: str) -> tuple[Wait, ...]:
        return tuple(w for w in self._waits.values() if w.run_id == run_id and w.satisfied)

    def _satisfy(self, wait: Wait, payload: dict[str, Any]) -> Wait:
        done = Wait(
            wait_id=wait.wait_id,
            run_id=wait.run_id,
            kind=wait.kind,
            state_snapshot=wait.state_snapshot,
            created_at=wait.created_at,
            satisfied=True,
            payload=dict(payload),
        )
        self._waits[wait.wait_id] = done
        return done


def resume(store: WaitStore, event: ResumeEvent) -> Wait:
    wait = store.get(event.wait_id)
    if wait is None:
        raise ResumeRejected(f"{event.wait_id} is not a known wait")
    if wait.run_id != event.run_id:
        raise ResumeRejected("resume event belongs to a different run")
    if wait.satisfied:
        raise ResumeRejected(f"{event.wait_id} was already satisfied")
    if wait.state_snapshot != event.state_snapshot:
        raise ResumeRejected(
            "the state this run was paused against has changed; re-ask rather than resume"
        )
    return store._satisfy(wait, event.payload)
