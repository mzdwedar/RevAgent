"""Traces that cross the whole stack, not just the model call (Part 8).

A trace that stops at the model call cannot answer why a run did what it did: the
decisions that mattered happened in context assembly, tool exposure, policy and
approval. `REQUIRED_SPANS` is the contract; removing a member is a bar change and
`scripts/stack_guard.py` flags it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

REQUIRED_SPANS: frozenset[str] = frozenset(
    {
        "run.start",
        "context.assemble",
        "model.call",
        "tool.expose",
        "policy.decide",
        "approval.request",
        "tool.call",
        "execution.commit",
        "response",
    }
)


@dataclass(frozen=True, slots=True)
class VersionStamp:
    """What a run was made of, so a trace can be reconstructed later (Part 8)."""

    prompt: str
    model: str
    tool_schema: str
    policy: str
    retrieval: str


@dataclass(slots=True)
class Span:
    name: str
    run_id: str
    session_id: str
    versions: VersionStamp
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Tracer:
    """Debug evidence. Not an audit trail, and not a verdict."""

    run_id: str
    session_id: str
    versions: VersionStamp
    spans: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        span = Span(
            name=name,
            run_id=self.run_id,
            session_id=self.session_id,
            versions=self.versions,
            attributes=dict(attributes),
        )
        self.spans.append(span)
        yield span

    def names(self) -> set[str]:
        return {span.name for span in self.spans}

    def missing_required(self) -> frozenset[str]:
        return REQUIRED_SPANS - self.names()
