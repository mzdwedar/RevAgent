"""Part 3: session identity is not user identity, and three stores stay three stores."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentstack.control_plane.resolve import SessionOwnershipError, SessionResolver
from agentstack.control_plane.session import Session, new_session
from agentstack.control_plane.stores import TranscriptStore, WorkingStateStore
from agentstack.interfaces.wiring import Stack


def test_a_session_keyed_on_the_user_id_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be the user id"):
        Session(
            session_id="u-1",
            user_id="u-1",
            tenant="acme",
            created_at=datetime.now(UTC),
        )


def test_one_user_can_own_several_isolated_sessions() -> None:
    a = new_session(user_id="u-1", tenant="acme")
    b = new_session(user_id="u-1", tenant="acme")
    assert a.session_id != b.session_id


def test_continuity_is_not_authorization() -> None:
    resolver = SessionResolver(transcripts=TranscriptStore(), working_state=WorkingStateStore())
    session = resolver.start(user_id="u-1", tenant="acme")
    with pytest.raises(SessionOwnershipError):
        resolver.resolve(session_id=session.session_id, user_id="u-2", tenant="acme")
    with pytest.raises(SessionOwnershipError):
        resolver.resolve(session_id=session.session_id, user_id="u-1", tenant="other")


def test_transcript_working_state_and_memory_are_distinct_stores(stack: Stack) -> None:
    assert stack.transcripts is not stack.working_state
    assert stack.transcripts is not stack.memory
    assert stack.working_state is not stack.memory
    assert type(stack.transcripts).__name__ == "TranscriptStore"
    assert type(stack.working_state).__name__ == "WorkingStateStore"
    assert type(stack.memory).__name__ == "MemoryStore"


def test_the_runtime_receives_a_bounded_view_not_the_record(stack: Stack) -> None:
    session = stack.resolver.start(user_id="u-1", tenant="acme")
    view = stack.resolver.resolve(session_id=session.session_id, user_id="u-1", tenant="acme")
    assert type(view).__name__ == "SessionView"
    assert not hasattr(view, "created_at"), "the view must not be the canonical record"
