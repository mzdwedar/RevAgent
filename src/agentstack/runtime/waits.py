"""Waiting is state, not sleep (Part 4).

Human approval, a webhook, a timer, a CI run, a long model job - each pauses a run
that must survive a process restart. A wait is therefore persisted, and a resume event
has to identify the run, the pending wait, the state it was paused against, and the
input that satisfies it. Anything less resumes into a world it never saw.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentstack.storage.database import Database


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


_COLUMNS = "wait_id, run_id, kind, state_snapshot, created_at, satisfied, payload"


@dataclass(frozen=True, slots=True)
class WaitStore:
    db: Database

    def park(self, *, run_id: str, kind: str, state_snapshot: str) -> Wait:
        row = self.db.fetch_one(
            "INSERT INTO waits (wait_id, run_id, kind, state_snapshot, created_at)"
            f" VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (f"wait-{uuid.uuid4()}", run_id, kind, state_snapshot, datetime.now(UTC)),
        )
        assert row is not None  # RETURNING on a successful insert always yields a row
        return Wait(*row)

    def get(self, wait_id: str) -> Wait | None:
        row = self.db.fetch_one(f"SELECT {_COLUMNS} FROM waits WHERE wait_id = %s", (wait_id,))
        return None if row is None else Wait(*row)

    def pending_for(self, run_id: str) -> tuple[Wait, ...]:
        rows = self.db.fetch_all(
            f"SELECT {_COLUMNS} FROM waits WHERE run_id = %s AND NOT satisfied ORDER BY created_at",
            (run_id,),
        )
        return tuple(Wait(*row) for row in rows)

    def satisfied_for(self, run_id: str) -> tuple[Wait, ...]:
        rows = self.db.fetch_all(
            f"SELECT {_COLUMNS} FROM waits WHERE run_id = %s AND satisfied"
            " ORDER BY satisfied_at, created_at",
            (run_id,),
        )
        return tuple(Wait(*row) for row in rows)

    def _satisfy(self, wait: Wait, payload: dict[str, Any]) -> Wait:
        """Mark the wait satisfied, once, even if two resumes arrive together.

        The checks in `resume` read and then decide, which is a race the moment two
        approvals land at the same moment. `AND NOT satisfied` in the UPDATE is what
        actually settles it: the loser gets no row back and is told so, rather than
        both callers being told they resumed the run.
        """
        row = self.db.fetch_one(
            "UPDATE waits SET satisfied = true, satisfied_at = now(), payload = %s::jsonb"
            f" WHERE wait_id = %s AND NOT satisfied RETURNING {_COLUMNS}",
            (json.dumps(payload), wait.wait_id),
        )
        if row is None:
            raise ResumeRejected(f"{wait.wait_id} was already satisfied")
        return Wait(*row)


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
