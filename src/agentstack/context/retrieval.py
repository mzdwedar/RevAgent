"""Retrieval supplies candidates (Part 5).

A similarity score means "this looks related". It does not mean "this should govern
the answer", and it says nothing about whether the reader is allowed to see it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agentstack.context.items import ContextItem, Scope, Trust


@dataclass(frozen=True, slots=True)
class Candidate:
    text: str
    score: float
    source: str
    scope: Scope
    observed_at: datetime
    trust: Trust = Trust.UNTRUSTED

    def as_context_item(self, reason: str) -> ContextItem:
        return ContextItem(
            kind="retrieved",
            text=self.text,
            scope=self.scope,
            provenance=f"retrieval:{self.source}",
            observed_at=self.observed_at,
            reason=reason,
            trust=self.trust,
        )


class Retriever(Protocol):
    def search(self, query: str, *, limit: int = 5) -> list[Candidate]: ...


@dataclass(slots=True)
class StaticRetriever:
    """Reference implementation: a fixed corpus, ranked by naive term overlap."""

    corpus: list[Candidate]

    def search(self, query: str, *, limit: int = 5) -> list[Candidate]:
        terms = set(query.lower().split())

        def overlap(candidate: Candidate) -> float:
            words = set(candidate.text.lower().split())
            return len(terms & words) / (len(terms) or 1)

        ranked = sorted(self.corpus, key=overlap, reverse=True)
        return [c for c in ranked if overlap(c) > 0][:limit]
