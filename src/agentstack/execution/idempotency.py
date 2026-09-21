"""Two-phase idempotency: claim the key, then finalize it (Part 4).

Retry, replay, resume and idempotency are four different things. This module is only
the fourth, and it gets one thing right that a naive version does not: **it records
intent before the effect, not success after it.**

A ledger whose only state is "already returned" cannot help with the failure that
matters. The surface applies the refund, the socket times out, nothing is recorded,
the run retries, and the money leaves twice. So there are three states, and the third
is the load-bearing one:

    FRESH      nobody has tried this key - go ahead
    IN_FLIGHT  claimed, never finalized - the outcome is UNKNOWN, not "not yet done"
    COMMITTED  finalized, with the receipt to return instead of acting again

`IN_FLIGHT` is never treated as permission to proceed. Something has to reconcile it
against the surface and then `finalize` or `abandon` it, because only the surface
knows what really happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class ClaimState(Enum):
    FRESH = "fresh"
    IN_FLIGHT = "in_flight"
    COMMITTED = "committed"


@dataclass(frozen=True, slots=True)
class Claim:
    state: ClaimState
    receipt: str | None = None
    claimed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Entry:
    key: str
    claimed_at: datetime
    receipt: str | None = None

    @property
    def settled(self) -> bool:
        return self.receipt is not None


@dataclass(slots=True)
class IdempotencyLedger:
    _entries: dict[str, _Entry] = field(default_factory=dict)

    def claim(self, key: str, *, now: datetime | None = None) -> Claim:
        """Stake the key before the effect. The answer decides whether to act."""
        existing = self._entries.get(key)
        if existing is None:
            self._entries[key] = _Entry(key=key, claimed_at=now or datetime.now(UTC))
            return Claim(ClaimState.FRESH)
        if existing.settled:
            return Claim(ClaimState.COMMITTED, receipt=existing.receipt)
        return Claim(ClaimState.IN_FLIGHT, claimed_at=existing.claimed_at)

    def finalize(self, key: str, receipt: str) -> None:
        """The surface answered. Record what it said."""
        existing = self._entries.get(key)
        claimed_at = existing.claimed_at if existing else datetime.now(UTC)
        self._entries[key] = _Entry(key=key, claimed_at=claimed_at, receipt=receipt)

    def abandon(self, key: str) -> None:
        """Release a claim for an attempt that provably did not apply.

        Only a caller that knows the effect did not happen may do this - a surface
        that refused before acting, or a reconciliation that checked. The gateway
        never calls it: from inside, a failure after dispatch is indistinguishable
        from a lost response.
        """
        self._entries.pop(key, None)

    def recorded(self, key: str) -> str | None:
        entry = self._entries.get(key)
        return entry.receipt if entry else None

    def unresolved_keys(self) -> tuple[str, ...]:
        """What a reconciliation job works through."""
        return tuple(k for k, e in self._entries.items() if not e.settled)

    def __len__(self) -> int:
        return len(self._entries)
