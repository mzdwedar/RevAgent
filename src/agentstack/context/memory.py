"""Durable state with a lifecycle - not a vector index, not the transcript (Context, retrieval,
memory).

Two rules this module exists to enforce:

1. **Writes are explicit.** Nothing is persisted because the model said it, a tool
   returned it, or a user mentioned it once. `write()` is the only door, and it
   demands scope, provenance and a TTL.
2. **Maintenance is off the hot path.** Extraction, consolidation, expiry and
   reindexing go on `MaintenanceQueue`; they do not block a turn.

Memory is also a trust boundary: a poisoned memory outlives the input that planted it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agentstack.context.items import ContextItem, Scope, Trust


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    key: str
    value: str
    scope: Scope
    provenance: str
    written_at: datetime
    ttl: timedelta
    trust: Trust

    def is_fresh(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) - self.written_at < self.ttl

    def as_context_item(self, reason: str) -> ContextItem:
        return ContextItem(
            kind="memory",
            text=f"{self.key}: {self.value}",
            scope=self.scope,
            provenance=self.provenance,
            observed_at=self.written_at,
            reason=reason,
            trust=self.trust,
        )


@dataclass(slots=True)
class MemoryStore:
    """Holds records. Does not decide what deserves to be one - `write()` does."""

    _records: list[MemoryRecord] = field(default_factory=list)

    def recall(self, scope: Scope, *, now: datetime | None = None) -> list[MemoryRecord]:
        return [r for r in self._records if scope.covers(r.scope) and r.is_fresh(now)]

    def all_records(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._records)

    def _append(self, record: MemoryRecord) -> None:
        """Private on purpose. Call `agentstack.context.memory.write()` instead."""
        self._records.append(record)


@dataclass(slots=True)
class MaintenanceQueue:
    """Extraction, consolidation, expiry, reindexing. Drained off the turn."""

    jobs: list[tuple[str, str]] = field(default_factory=list)

    def enqueue(self, job: str, subject: str) -> None:
        self.jobs.append((job, subject))


def write(
    store: MemoryStore,
    *,
    key: str,
    value: str,
    scope: Scope,
    provenance: str,
    ttl: timedelta,
    explicit: bool,
    trust: Trust = Trust.FIRST_PARTY,
    now: datetime | None = None,
) -> MemoryRecord:
    """The only way anything becomes memory.

    `explicit=True` is not ceremony: it is the difference between a decision someone
    made and a side effect of the model having said something.
    """
    if not explicit:
        raise ValueError(
            "memory writes must be explicit (Context, retrieval, memory): pass explicit=True "
            "at a point where someone decided this deserves to persist"
        )
    if not provenance:
        raise ValueError("memory writes require provenance")
    if ttl <= timedelta(0):
        raise ValueError(
            "memory writes require a positive TTL; nothing persists forever by default"
        )
    record = MemoryRecord(
        key=key,
        value=value,
        scope=scope,
        provenance=provenance,
        written_at=now or datetime.now(UTC),
        ttl=ttl,
        trust=trust,
    )
    store._append(record)
    return record
