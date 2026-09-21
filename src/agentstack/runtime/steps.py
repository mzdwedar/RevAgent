"""Step boundaries: recorded progress, so a retry knows what already happened.

Retrying the whole agent after a partial side effect is the classic Part 4 failure:
the branch was already pushed, step six failed, the retry pushes again. A step records
its completion, and a completed step is not re-run on replay.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class StepRecord:
    run_id: str
    name: str
    status: str
    receipt: str | None
    at: datetime


@dataclass(slots=True)
class StepLedger:
    _records: list[StepRecord] = field(default_factory=list)

    def completed(self, run_id: str, name: str) -> StepRecord | None:
        for record in self._records:
            if record.run_id == run_id and record.name == name and record.status == "completed":
                return record
        return None

    def records_for(self, run_id: str) -> tuple[StepRecord, ...]:
        return tuple(r for r in self._records if r.run_id == run_id)

    def _write(self, run_id: str, name: str, status: str, receipt: str | None) -> StepRecord:
        record = StepRecord(
            run_id=run_id, name=name, status=status, receipt=receipt, at=datetime.now(UTC)
        )
        self._records.append(record)
        return record

    @contextmanager
    def step(self, run_id: str, name: str) -> Iterator[list[str | None]]:
        """Run a side-effecting step once, recording completion.

        Yields a one-element list the body sets to the receipt. On replay the body is
        skipped entirely - that is what makes the boundary worth having.
        """
        already = self.completed(run_id, name)
        if already is not None:
            yield [already.receipt]
            return
        slot: list[str | None] = [None]
        self._write(run_id, name, "started", None)
        try:
            yield slot
        except Exception:
            self._write(run_id, name, "failed", None)
            raise
        self._write(run_id, name, "completed", slot[0])
