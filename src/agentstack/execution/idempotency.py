"""Two-phase idempotency: claim the key, then finalize it (Part 4).

Retry, replay, resume and idempotency are four different things. This module is only
the fourth, and it gets one thing right that a naive version does not: **it records
intent before the effect, not success after it.**

A ledger whose only state is "already returned" cannot help with the failure that
matters. The surface applies the rollout, the socket times out, nothing is recorded,
the run retries, and the customers are exposed twice. So there are three states, and the third
is the load-bearing one:

    FRESH      nobody has tried this key - go ahead
    IN_FLIGHT  claimed, never finalized - the outcome is UNKNOWN, not "not yet done"
    COMMITTED  finalized, with the receipt to return instead of acting again

`IN_FLIGHT` is never treated as permission to proceed. Something has to reconcile it
against the surface and then `finalize` or `abandon` it, because only the surface
knows what really happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from agentstack.storage.database import Database


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
class IdempotencyLedger:
    db: Database

    def claim(self, key: str, *, now: datetime | None = None) -> Claim:
        """Stake the key before the effect. The answer decides whether to act.

        One statement, not a read and then an insert. Two runs reaching the same key
        together both read "nothing here" and both commit; the insert is the only
        thing that can decide between them.

        `DO UPDATE SET key = EXCLUDED.key` is a no-op write whose purpose is to make
        the conflicting row visible to RETURNING - `DO NOTHING` returns nothing, which
        would need a follow-up SELECT and reopen a smaller version of the same window.
        `xmax = 0` is true only for a row this statement inserted, which is how the
        winner learns it won.
        """
        row = self.db.fetch_one(
            "INSERT INTO idempotency_claims (key, claimed_at) VALUES (%s, %s)"
            " ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key"
            " RETURNING claimed_at, receipt, (xmax = 0) AS inserted",
            (key, now or datetime.now(UTC)),
        )
        assert row is not None  # the upsert always returns exactly one row
        claimed_at, receipt, inserted = row
        if inserted:
            # When it was staked names this claim: a reconcile wait for it is one per
            # claim, not one per key, because a key released and claimed again is a
            # second attempt whose outcome can be unknown in its own right.
            return Claim(ClaimState.FRESH, claimed_at=claimed_at)
        if receipt is not None:
            return Claim(ClaimState.COMMITTED, receipt=receipt)
        return Claim(ClaimState.IN_FLIGHT, claimed_at=claimed_at)

    def finalize(self, key: str, receipt: str) -> None:
        """The surface answered. Record what it said."""
        self.db.execute(
            "INSERT INTO idempotency_claims (key, claimed_at, receipt, settled_at)"
            " VALUES (%s, now(), %s, now())"
            " ON CONFLICT (key) DO UPDATE SET receipt = EXCLUDED.receipt, settled_at = now()",
            (key, receipt),
        )

    def abandon(self, key: str) -> None:
        """Release a claim for an attempt that provably did not apply.

        Only a caller that knows the effect did not happen may do this - a surface
        that refused before acting, or a reconciliation that checked. The gateway
        never calls it: from inside, a failure after dispatch is indistinguishable
        from a lost response.
        """
        self.db.execute("DELETE FROM idempotency_claims WHERE key = %s", (key,))

    def inspect(self, key: str) -> Claim | None:
        """What the ledger says about `key`, without staking it. None: nobody holds it."""
        row = self.db.fetch_one(
            "SELECT claimed_at, receipt FROM idempotency_claims WHERE key = %s", (key,)
        )
        if row is None:
            return None
        claimed_at, receipt = row
        if receipt is not None:
            return Claim(ClaimState.COMMITTED, receipt=receipt, claimed_at=claimed_at)
        return Claim(ClaimState.IN_FLIGHT, claimed_at=claimed_at)

    def reconcile(self, key: str, *, receipt: str | None) -> bool:
        """Settle an unresolved claim with what the surface was found to have done.

        `receipt` is what the surface says it did; `None` means someone checked and it did
        not apply, which releases the key like `abandon`. Only an unresolved claim moves:
        the condition is in the same statement as the change, so two reconcilers racing
        (one saying applied, one not) cannot both win, and a claim the gateway already
        settled is never overwritten. True when this call settled it.
        """
        if receipt is not None:
            row = self.db.fetch_one(
                "UPDATE idempotency_claims SET receipt = %s, settled_at = now()"
                " WHERE key = %s AND receipt IS NULL RETURNING key",
                (receipt, key),
            )
        else:
            row = self.db.fetch_one(
                "DELETE FROM idempotency_claims WHERE key = %s AND receipt IS NULL RETURNING key",
                (key,),
            )
        return row is not None

    def recorded(self, key: str) -> str | None:
        row = self.db.fetch_one("SELECT receipt FROM idempotency_claims WHERE key = %s", (key,))
        return None if row is None else row[0]

    def unresolved_keys(self) -> tuple[str, ...]:
        """What a reconciliation job works through."""
        rows = self.db.fetch_all(
            "SELECT key FROM idempotency_claims WHERE receipt IS NULL ORDER BY claimed_at"
        )
        return tuple(row[0] for row in rows)

    def __len__(self) -> int:
        row = self.db.fetch_one("SELECT count(*) FROM idempotency_claims")
        return 0 if row is None else int(row[0])
