"""Step boundaries: recorded progress, so a retry knows what already happened.

Retrying the whole agent after a partial side effect is the classic Part 4 failure:
the branch was already pushed, step six failed, the retry pushes again. A step records
its completion, and a completed step is not re-run on replay.

Durable since T3. In memory the guarantee held only within one process, which is the
single case where retry never needed it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from agentstack.storage.database import Database, IntegrityViolation

# The database's own statement that a step completes once (migrations/0002).
COMPLETE_ONCE = "run_steps_complete_once"


def execute_step_name(tool: str, fingerprint: str) -> str:
    """The step an act is recorded under: the action's identity, not just the tool's.

    One name for both paths that commit - the turn, and the commit activity after an
    approval - so the second reads as the continuation of the first in the ledger.
    """
    return f"execute:{tool}:{fingerprint}"


class StepConflict(RuntimeError):
    """Two workers completed one step with different receipts: two effects, not one."""


@dataclass(frozen=True, slots=True)
class StepRecord:
    run_id: str
    name: str
    status: str
    receipt: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class StepLedger:
    db: Database

    def completed(self, run_id: str, name: str) -> StepRecord | None:
        row = self.db.fetch_one(
            "SELECT run_id, name, status, receipt, at FROM run_steps"
            " WHERE run_id = %s AND name = %s AND status = 'completed'",
            (run_id, name),
        )
        return None if row is None else StepRecord(*row)

    def records_for(self, run_id: str) -> tuple[StepRecord, ...]:
        rows = self.db.fetch_all(
            "SELECT run_id, name, status, receipt, at FROM run_steps WHERE run_id = %s ORDER BY id",
            (run_id,),
        )
        return tuple(StepRecord(*row) for row in rows)

    def started_but_unfinished(self, run_id: str) -> tuple[StepRecord, ...]:
        """Steps that began and never reported an outcome.

        A process that died mid-step leaves one of these. It is not a failure record:
        the effect may or may not have landed, and the only honest thing the ledger
        can say is that nobody knows. The idempotency ledger settles it.
        """
        rows = self.db.fetch_all(
            "SELECT s.run_id, s.name, s.status, s.receipt, s.at FROM run_steps s"
            " WHERE s.run_id = %s AND s.status = 'started'"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM run_steps done"
            "   WHERE done.run_id = s.run_id AND done.name = s.name"
            # A completed step is finished, whichever worker's `started` row came
            # last: two deliveries racing one step leave the loser's `started` after
            # the winner's `completed` (T21).
            "     AND (done.status = 'completed'"
            "          OR (done.status <> 'started' AND done.id > s.id)))"
            " ORDER BY s.id",
            (run_id,),
        )
        return tuple(StepRecord(*row) for row in rows)

    def _write(self, run_id: str, name: str, status: str, receipt: str | None) -> StepRecord:
        row = self.db.fetch_one(
            "INSERT INTO run_steps (run_id, name, status, receipt) VALUES (%s, %s, %s, %s)"
            " RETURNING run_id, name, status, receipt, at",
            (run_id, name, status, receipt),
        )
        assert row is not None  # RETURNING on a successful insert always yields a row
        return StepRecord(*row)

    @contextmanager
    def step(
        self,
        run_id: str,
        name: str,
        *,
        pause_on: tuple[type[Exception], ...] = (),
    ) -> Iterator[list[str | None]]:
        """Run a side-effecting step once, recording completion.

        Yields a one-element list the body sets to the receipt. On replay the body is
        skipped entirely - that is what makes the boundary worth having.

        Two deliveries of one turn can both pass the `completed` check before either
        writes (T21). The effect is still applied once - the idempotency ledger hands
        the loser the winner's receipt - and the database lets only one completion
        land. The loser is told the step is done rather than crashing on the
        constraint, provided it saw the same receipt; a different one is two effects,
        and that is refused loudly.

        `pause_on` names the exceptions that are a designed pause, not a fault: the step
        is recorded `awaiting_approval` instead of `failed`. The caller says so because
        only the caller knows whether a wait is parked. On the turn path it is; at the
        commit activity a refusal is terminal and nothing is waiting, so it is `failed`.
        """
        already = self.completed(run_id, name)
        if already is not None:
            yield [already.receipt]
            return
        slot: list[str | None] = [None]
        self._write(run_id, name, "started", None)
        try:
            yield slot
        except pause_on:
            # The caller has parked a wait and the same step runs again once a person
            # has answered.
            self._write(run_id, name, "awaiting_approval", None)
            raise
        except Exception:
            self._write(run_id, name, "failed", None)
            raise
        try:
            self._write(run_id, name, "completed", slot[0])
        except IntegrityViolation as exc:
            if exc.constraint != COMPLETE_ONCE:
                raise
            winner = self.completed(run_id, name)
            assert winner is not None  # the constraint only fires on an existing completion
            if winner.receipt != slot[0]:
                raise StepConflict(
                    f"step {name!r} of {run_id} was completed concurrently with receipt "
                    f"{winner.receipt!r}, and this worker's effect returned {slot[0]!r}: "
                    "two effects where the step boundary promised one"
                ) from exc
