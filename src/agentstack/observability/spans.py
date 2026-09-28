"""Traces that cross the whole stack, not just the model call (Part 8).

A trace that stops at the model call cannot answer why a run did what it did: the
decisions that mattered happened in context assembly, tool exposure, policy and
approval. `REQUIRED_SPANS` is the contract; removing a member is a bar change and
`scripts/stack_guard.py` flags it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

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


class SpanSink(Protocol):
    """Where spans go when nobody who asked for the work can hold them.

    A turn run by `handle` returns its tracer to its caller. A turn run in a Temporal
    activity has no such caller: the workflow must never hold spans, because what it
    holds is written into history. So the activity exports them here, and the
    composition root decides where "here" is. Never the audit sink: traces explain,
    audit records account (Part 8).
    """

    def export(self, spans: Sequence[Span]) -> None: ...


@dataclass(slots=True)
class CollectingSink:
    """Keeps every span it's given, so a test can read what a worker process emitted."""

    spans: list[Span] = field(default_factory=list)

    def export(self, spans: Sequence[Span]) -> None:
        self.spans.extend(spans)

    def for_run(self, run_id: str) -> list[Span]:
        return [span for span in self.spans if span.run_id == run_id]


@dataclass(frozen=True, slots=True)
class LoggingSink:
    """One JSON line per span, on a named logger: what the worker exports until a trace
    backend is chosen. The logger is the seam; where it ships is deployment's choice."""

    logger: str = "agentstack.traces"

    def export(self, spans: Sequence[Span]) -> None:
        log = logging.getLogger(self.logger)
        for span in spans:
            log.info(
                json.dumps(
                    {
                        "span": span.name,
                        "run_id": span.run_id,
                        "session_id": span.session_id,
                        "versions": asdict(span.versions),
                        "attributes": span.attributes,
                    },
                    default=str,
                    sort_keys=True,
                )
            )
