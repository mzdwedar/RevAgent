"""Waiting is state, not sleep (Part 4).

Human approval, a webhook, a timer, a CI run, a long model job - each pauses a run
that must survive a process restart. A wait is therefore persisted, and a resume event
has to identify the run, the pending wait, the state it was paused against, and the
input that satisfies it. Anything less resumes into a world it never saw.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agentstack.storage.database import Database


class ResumeRejected(RuntimeError):
    """The resume event does not match a pending wait, or the world moved."""


class WaitWithoutDeadline(ValueError):
    """A wait that can go unanswered forever was parked with no time it is due by."""


TRIGGER = "trigger"
HUMAN_APPROVAL = "human_approval"
# A run whose checkpoint this code cannot read. Not stalled - nothing is late, the code
# moved - and satisfied by migrating the checkpoint, not by an event arriving.
NEEDS_MIGRATION = "needs_migration"
# An effect whose outcome is unknown: the surface may have applied it (E2). Satisfied by
# whoever reconciles the idempotency claim against the surface, never by waiting.
RECONCILE = "reconcile"

# How long a question sits unanswered before it is put again. A default, unlike a
# trigger's deadline: how patiently to treat a person does not depend on the workflow,
# whereas how long to wait for data depends entirely on how often the data arrives.
APPROVAL_REASK_AFTER = timedelta(hours=24)

# How soon an effect of unknown outcome should have been reconciled. Short, because the
# effect may be live (a rollout customers can already see) and the run, and every
# trigger queued behind it, waits until someone looks. `operator stalled` reports a
# reconcile wait from the moment it is parked, since it never resolves by waiting; past
# this it is reported as overdue.
RECONCILE_DUE_AFTER = timedelta(hours=1)


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
    # What a human approval wait is *about*. The process that parks the wait holds the
    # prepared request and the rendered prompt in memory; the one that handles the
    # answer an hour later holds neither, and cannot bind an approval to a fingerprint
    # it never computed.
    action_fingerprint: str | None = None
    approval_summary: str | None = None
    # When this wait should have been satisfied by. Past it, a trigger wait is stalled
    # and an approval wait is asked again; neither is allowed to lapse quietly.
    deadline: datetime | None = None
    reasks: int = 0
    # The action itself: the tool and its validated arguments (0018). The fingerprint
    # says *which* action; this is the action, so the process that commits it reads it
    # from the record and not from a turn's checkpoint, whose shape can change under it.
    action_tool: str | None = None
    action_arguments: dict[str, Any] | None = None
    # What a reconcile wait is about: the idempotency claim a person has to settle
    # against the surface. The fingerprint says which action; this says which claim.
    idempotency_key: str | None = None


def approval_wait_id(run_id: str, action_fingerprint: str, state_snapshot: str) -> str:
    """The one approval wait for this action, against this state, in this run.

    Derived, so a turn that parks it and dies before its checkpoint lands parks the
    same wait when it runs again, not a second question. A different state is a
    different question: an approval is bound to the world it was asked about.
    """
    digest = hashlib.sha256(f"{run_id}\n{action_fingerprint}\n{state_snapshot}".encode())
    return f"wait-approval-{digest.hexdigest()[:24]}"


def reconcile_wait_id(run_id: str, idempotency_key: str, claimed_at: datetime | None) -> str:
    """The one reconcile wait for this claim in this run: a rerun parks it again, not a
    second one. Not bound to a snapshot: what is unknown is the effect, whatever the world
    was.

    One per *claim*, not per action. Reconciled as not applied, the key is released and
    the act goes again; if that attempt's answer is lost too, it is a new unknown, and it
    needs a wait of its own. Reusing the first (already satisfied) one would read as
    answered, and the run would spin on the claim instead of stopping for a person.
    """
    staked = "" if claimed_at is None else claimed_at.astimezone(UTC).isoformat()
    digest = hashlib.sha256(f"{run_id}\n{idempotency_key}\n{staked}".encode())
    return f"wait-reconcile-{digest.hexdigest()[:24]}"


def park_reconcile(
    store: WaitStore,
    *,
    run_id: str,
    action_fingerprint: str,
    idempotency_key: str,
    claimed_at: datetime | None,
    state_snapshot: str,
) -> Wait:
    """Park the run on an effect of unknown outcome (E2), wherever it was met.

    The turn and the commit both land here, so a reconcile wait is the same thing from
    either: named for its claim, due `RECONCILE_DUE_AFTER` from now, and saying which
    action and which claim a person has to settle.
    """
    return store.park(
        wait_id=reconcile_wait_id(run_id, idempotency_key, claimed_at),
        run_id=run_id,
        kind=RECONCILE,
        state_snapshot=state_snapshot,
        action_fingerprint=action_fingerprint,
        idempotency_key=idempotency_key,
    )


@dataclass(frozen=True, slots=True)
class ResumeEvent:
    run_id: str
    wait_id: str
    state_snapshot: str
    payload: dict[str, Any]


_COLUMNS = (
    "wait_id, run_id, kind, state_snapshot, created_at, satisfied, payload, "
    "action_fingerprint, approval_summary, deadline, reasks, action_tool, action_arguments, "
    "idempotency_key"
)


@dataclass(frozen=True, slots=True)
class WaitStore:
    db: Database

    def park(
        self,
        *,
        run_id: str,
        kind: str,
        state_snapshot: str,
        action_fingerprint: str | None = None,
        approval_summary: str | None = None,
        timeout: timedelta | None = None,
        wait_id: str | None = None,
        action_tool: str | None = None,
        action_arguments: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Wait:
        """Persist a wait, due `timeout` from now.

        A trigger wait has no default timeout. The right one is "a little longer than
        the data normally takes to arrive", which only the caller knows, and a default
        chosen here would be a guess that looks like a decision.

        `wait_id` is for a caller that may park the same wait twice: an activity rerun
        after its first attempt landed. Parking an id that's already there returns the
        wait as it was first parked, deadline included, instead of a second one.

        A reconcile wait names the claim it is about (`idempotency_key`): the person who
        settles it has to know which one, and the database refuses one that doesn't say.
        """
        if timeout is None and kind == HUMAN_APPROVAL:
            timeout = APPROVAL_REASK_AFTER
        if timeout is None and kind == RECONCILE:
            timeout = RECONCILE_DUE_AFTER
        if timeout is None and kind == TRIGGER:
            raise WaitWithoutDeadline(
                "a trigger wait needs a deadline: a trigger that never fires must "
                "surface as stalled, not leave the run asleep"
            )
        if timeout is not None and timeout <= timedelta(0):
            raise WaitWithoutDeadline(f"a wait cannot be due {timeout} after it was parked")
        now = datetime.now(UTC)
        wait_id = wait_id or f"wait-{uuid.uuid4()}"
        row = self.db.fetch_one(
            "INSERT INTO waits (wait_id, run_id, kind, state_snapshot, created_at,"
            "  action_fingerprint, approval_summary, deadline, action_tool, action_arguments,"
            "  idempotency_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)"
            f" ON CONFLICT (wait_id) DO NOTHING RETURNING {_COLUMNS}",
            (
                wait_id,
                run_id,
                kind,
                state_snapshot,
                now,
                action_fingerprint,
                approval_summary,
                None if timeout is None else now + timeout,
                action_tool,
                None if action_arguments is None else json.dumps(action_arguments),
                idempotency_key,
            ),
        )
        if row is None:
            # Parked already, by an earlier attempt: that wait, as it was parked.
            existing = self.get(wait_id)
            assert existing is not None  # the conflict was on this id
            return existing
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

    def stalled(self, *, now: datetime, older_than: timedelta = timedelta(0)) -> tuple[Wait, ...]:
        """Runs that will not move on their own, parked at least `older_than` before `now`.

        Three kinds, reported together and told apart by `kind` because the remedies
        differ: a trigger wait past its deadline (find out why the data never came), a
        run in `needs_migration` (migrate its checkpoint), which has no deadline since it
        was never going to resume by waiting, and a `reconcile` wait (settle the claim
        against the surface: `agentstack-operator reconcile`). A reconcile wait is
        reported as soon as it is parked, for the same reason as a migration: nothing
        settles it but a person. Its deadline says when it became overdue.

        The deadline is what makes a trigger wait stalled; `older_than` only narrows the
        report. `now` is an argument so that "a week later" is something a test can say.
        """
        rows = self.db.fetch_all(
            f"SELECT {_COLUMNS} FROM waits"
            " WHERE NOT satisfied AND created_at <= %s"
            "   AND ((kind = %s AND deadline <= %s) OR kind IN (%s, %s))"
            " ORDER BY kind, deadline, created_at",
            (now - older_than, TRIGGER, now, NEEDS_MIGRATION, RECONCILE),
        )
        return tuple(Wait(*row) for row in rows)

    def due_for_reask(self, *, now: datetime) -> tuple[Wait, ...]:
        rows = self.db.fetch_all(
            f"SELECT {_COLUMNS} FROM waits"
            " WHERE NOT satisfied AND kind = %s AND deadline <= %s"
            " ORDER BY deadline, created_at",
            (HUMAN_APPROVAL, now),
        )
        return tuple(Wait(*row) for row in rows)

    def record_asked(self, wait_id: str, *, reasks: int, next_deadline: datetime) -> Wait | None:
        """Record that the question was put for the `reasks`-th time again, and when next.

        Only moves forward, and only while the wait is pending: a rerun of the same ask
        (at-least-once) finds `reasks` already there and changes nothing, and an answer
        that landed first wins. `None` means one of those happened.
        """
        row = self.db.fetch_one(
            "UPDATE waits SET deadline = %s, reasks = %s"
            " WHERE wait_id = %s AND NOT satisfied AND reasks < %s"
            f" RETURNING {_COLUMNS}",
            (next_deadline, reasks, wait_id, reasks),
        )
        return None if row is None else Wait(*row)

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
