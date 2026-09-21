"""Recorded completion, so a retry does not repeat a committed side effect (Part 4).

Retry, replay, resume and idempotency are four different things. This module is only
the fourth: given the same key, the effect happens once and later attempts return the
recorded receipt instead of doing it again.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class IdempotencyLedger:
    _receipts: dict[str, str] = field(default_factory=dict)

    def recorded(self, key: str) -> str | None:
        return self._receipts.get(key)

    def record(self, key: str, receipt: str) -> None:
        self._receipts[key] = receipt

    def __len__(self) -> int:
        return len(self._receipts)
