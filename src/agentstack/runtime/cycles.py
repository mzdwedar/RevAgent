"""One evaluation cycle per trigger, however many times it is delivered.

Brokers redeliver. Without a claim, a redelivered trigger scores the cohort again,
drafts again, and asks a human again about a decision they already made - and the
second ask arrives with no sign that it is the same question.

The claim is one statement, for the reason T4 spelled out: two deliveries arriving
together both read "nothing here" and both proceed, and the insert is the only thing
that can decide between them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agentstack.policy.triggers import Outcome, TriggerEvent, TriggerKind, authorize
from agentstack.storage.database import Database

_COLUMNS = "experiment_id, data_as_of, kind, outcome, run_id, claimed_at, settled_at"


@dataclass(frozen=True, slots=True)
class Cycle:
    experiment_id: str
    data_as_of: str
    kind: TriggerKind
    outcome: Outcome | None
    run_id: str | None
    claimed_at: datetime
    settled_at: datetime | None

    @property
    def settled(self) -> bool:
        return self.outcome is not None

    @staticmethod
    def of(row: tuple[Any, ...]) -> Cycle:
        experiment_id, data_as_of, kind, outcome, run_id, claimed_at, settled_at = row
        return Cycle(
            experiment_id=str(experiment_id),
            data_as_of=str(data_as_of),
            kind=TriggerKind(kind),
            outcome=None if outcome is None else Outcome(outcome),
            run_id=None if run_id is None else str(run_id),
            claimed_at=claimed_at,
            settled_at=settled_at,
        )


@dataclass(frozen=True, slots=True)
class CycleStore:
    db: Database

    def claim(self, trigger: TriggerEvent) -> tuple[Cycle, bool]:
        """Stake this (experiment, watermark, kind), and say whether we got it."""
        row = self.db.fetch_one(
            "INSERT INTO trigger_cycles (experiment_id, data_as_of, kind)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (experiment_id, data_as_of, kind) DO UPDATE"
            "   SET experiment_id = EXCLUDED.experiment_id"
            f" RETURNING {_COLUMNS}, (xmax = 0) AS inserted",
            (trigger.experiment_id, trigger.data_as_of, trigger.kind.value),
        )
        assert row is not None  # the upsert always returns exactly one row
        return Cycle.of(row[:-1]), bool(row[-1])

    def settle(self, trigger: TriggerEvent, outcome: Outcome, run_id: str | None) -> Cycle:
        row = self.db.fetch_one(
            "UPDATE trigger_cycles SET outcome = %s, run_id = %s, settled_at = now()"
            " WHERE experiment_id = %s AND data_as_of = %s AND kind = %s"
            f" RETURNING {_COLUMNS}",
            (outcome.value, run_id, trigger.experiment_id, trigger.data_as_of, trigger.kind.value),
        )
        assert row is not None  # settle only follows a successful claim
        return Cycle.of(row)

    def get(self, trigger: TriggerEvent) -> Cycle | None:
        row = self.db.fetch_one(
            f"SELECT {_COLUMNS} FROM trigger_cycles"
            " WHERE experiment_id = %s AND data_as_of = %s AND kind = %s",
            (trigger.experiment_id, trigger.data_as_of, trigger.kind.value),
        )
        return None if row is None else Cycle.of(row)

    def unsettled(self) -> tuple[Cycle, ...]:
        """Cycles that began and never reached an outcome.

        The same shape as an unresolved idempotency claim, and for the same reason: an
        evaluation that stopped halfway is not a "no", and treating it as one is how a
        run silently skips a watermark.
        """
        rows = self.db.fetch_all(
            f"SELECT {_COLUMNS} FROM trigger_cycles WHERE outcome IS NULL ORDER BY claimed_at"
        )
        return tuple(Cycle.of(row) for row in rows)


Evaluator = Callable[[TriggerEvent], tuple[Outcome, str | None]]


def evaluate(store: CycleStore, trigger: TriggerEvent, evaluator: Evaluator) -> Cycle:
    """Run one cycle for this trigger, or return the one that already ran.

    An outcome is authorised against the kind that produced it *before* it is recorded,
    because the evaluator is the thing that might be wrong. If that check refuses, the
    cycle stays claimed and unsettled rather than being recorded as some safer outcome -
    inventing a decision to tidy up a refusal is worse than leaving it visible.
    """
    cycle, fresh = store.claim(trigger)
    if not fresh:
        return cycle
    outcome, run_id = evaluator(trigger)
    authorize(trigger.kind, outcome)
    return store.settle(trigger, outcome, run_id)
