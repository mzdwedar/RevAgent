"""Approval timeout: re-ask, never silent expiry (SPEC.md, iteration-1 decisions).

An approval nobody answers is not a refusal and not a consent. Expiring it quietly
would turn "the approver was on holiday" into "the run ended", and nobody would learn
that a decision had been dropped. So an overdue approval is put again, and its deadline
moves on by the same interval.

The asking itself is a seam. This layer cannot import the channel that carries the
question (contract 4), and should not know that it is Slack.

**Ask first, then move the deadline.** If the process dies between the two, the
question is put twice; the other order would lose it for a whole interval. A second
copy of a question is harmless - the second answer to one wait is refused as "already
answered" - whereas a missing one is exactly the silent expiry this exists to prevent.
Two timers firing together can also both ask, for the same reason and with the same
consequence; `record_reask` makes sure only one of them moves the deadline.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from agentstack.runtime.waits import APPROVAL_REASK_AFTER, Wait, WaitStore

Ask = Callable[[Wait], None]


class ReaskFailed(RuntimeError):
    """At least one overdue question could not be put again. Its deadline has not moved."""

    def __init__(self, failures: dict[str, Exception]) -> None:
        detail = "; ".join(f"{wait_id}: {exc}" for wait_id, exc in failures.items())
        super().__init__(
            f"{len(failures)} overdue approval(s) could not be re-asked and are still due: {detail}"
        )
        self.failures = failures


def fire_reasks(
    store: WaitStore,
    *,
    now: datetime,
    ask: Ask,
    every: timedelta = APPROVAL_REASK_AFTER,
) -> tuple[Wait, ...]:
    """Put every overdue approval again, and return the waits as they now stand.

    One failed ask does not stop the others from being asked; it is raised after them,
    with its deadline left where it was so the next tick tries again.
    """
    reasked: list[Wait] = []
    failures: dict[str, Exception] = {}
    for wait in store.due_for_reask(now=now):
        try:
            ask(wait)
        except Exception as exc:
            failures[wait.wait_id] = exc
            continue
        moved = store.record_reask(wait, next_deadline=now + every)
        if moved is not None:
            reasked.append(moved)
    if failures:
        raise ReaskFailed(failures)
    return tuple(reasked)
