"""Transcript state and working state - two stores, named (Part 3).

The transcript is the durable record of what occurred. The working state is the
mutable scratchpad for the live interaction. Memory is neither, and lives in
`agentstack.context.memory`. Worker-local state is not a third option: a restart
would take continuity with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    session_id: str
    kind: str
    body: str
    at: datetime


@dataclass(slots=True)
class TranscriptStore:
    """The authoritative record. Prompt context is derived from it, never equal to it."""

    _events: list[TranscriptEvent] = field(default_factory=list)

    def append(self, *, session_id: str, kind: str, body: str) -> TranscriptEvent:
        event = TranscriptEvent(session_id=session_id, kind=kind, body=body, at=datetime.now(UTC))
        self._events.append(event)
        return event

    def for_session(self, session_id: str) -> tuple[TranscriptEvent, ...]:
        return tuple(e for e in self._events if e.session_id == session_id)


@dataclass(slots=True)
class WorkingStateStore:
    """Ordered, rewindable scratch state. Never the place to record a side effect."""

    _state: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, session_id: str) -> dict[str, Any]:
        return dict(self._state.get(session_id, {}))

    def put(self, session_id: str, key: str, value: Any) -> None:
        self._state.setdefault(session_id, {})[key] = value
