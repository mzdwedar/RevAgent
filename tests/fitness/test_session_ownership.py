"""Part 3: session identity is not user identity, and three stores stay three stores.

Since T2 these run against Postgres. That matters more than it sounds: the properties
here are about what survives, and an in-memory store cannot fail the test that asks.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from agentstack.control_plane.resolve import SessionOwnershipError
from agentstack.control_plane.session import Session, new_session
from agentstack.interfaces.wiring import Stack, build_stack
from agentstack.storage.database import Database

TENANT = "acme"


def test_a_session_keyed_on_the_user_id_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be the user id"):
        Session(
            session_id="u-1",
            user_id="u-1",
            tenant="acme",
            created_at=datetime.now(UTC),
        )


def test_the_database_refuses_it_too_not_just_the_constructor(app_database: Database) -> None:
    """A constructor guards the path that goes through it. A CHECK guards the rest."""
    with pytest.raises(psycopg.errors.CheckViolation, match="session_id_is_not_the_user_id"):
        app_database.execute(
            "INSERT INTO sessions (session_id, user_id, tenant, created_at)"
            " VALUES (%s, %s, %s, now())",
            ("u-1", "u-1", TENANT),
        )


def test_one_user_can_own_several_isolated_sessions() -> None:
    a = new_session(user_id="u-1", tenant="acme")
    b = new_session(user_id="u-1", tenant="acme")
    assert a.session_id != b.session_id


def test_continuity_is_not_authorization(stack: Stack) -> None:
    session = stack.resolver.start(user_id="u-1", tenant=TENANT)
    with pytest.raises(SessionOwnershipError):
        stack.resolver.resolve(session_id=session.session_id, user_id="u-2", tenant=TENANT)
    with pytest.raises(SessionOwnershipError):
        stack.resolver.resolve(session_id=session.session_id, user_id="u-1", tenant="other")


def test_an_unknown_session_is_refused_rather_than_created(stack: Stack) -> None:
    with pytest.raises(SessionOwnershipError, match="not a known session"):
        stack.resolver.resolve(session_id="sess-nope", user_id="u-1", tenant=TENANT)


def test_transcript_working_state_and_memory_are_distinct_stores(stack: Stack) -> None:
    assert stack.transcripts is not stack.working_state
    assert stack.transcripts is not stack.memory
    assert stack.working_state is not stack.memory
    assert type(stack.transcripts).__name__ == "TranscriptStore"
    assert type(stack.working_state).__name__ == "WorkingStateStore"
    assert type(stack.memory).__name__ == "MemoryStore"


def test_they_are_three_tables_as_well_as_three_classes(app_database: Database) -> None:
    """One table with a `kind` column is how the transcript becomes the context window."""
    rows = app_database.fetch_all(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    tables = {row[0] for row in rows}

    assert {"sessions", "transcript_events", "working_state"} <= tables


def test_the_runtime_receives_a_bounded_view_not_the_record(stack: Stack) -> None:
    session = stack.resolver.start(user_id="u-1", tenant=TENANT)
    view = stack.resolver.resolve(session_id=session.session_id, user_id="u-1", tenant=TENANT)
    assert type(view).__name__ == "SessionView"
    assert not hasattr(view, "created_at"), "the view must not be the canonical record"


def test_a_session_outlives_the_process_that_started_it(
    stack: Stack, app_database: Database
) -> None:
    """The point of T2. A second stack shares nothing with the first but the database."""
    session = stack.resolver.start(user_id="u-1", tenant=TENANT)
    stack.transcripts.append(session_id=session.session_id, kind="user", body="first")

    restarted = build_stack(app_database, tenant=TENANT)
    view = restarted.resolver.resolve(session_id=session.session_id, user_id="u-1", tenant=TENANT)

    assert view.session_id == session.session_id
    assert [e.body for e in restarted.transcripts.for_session(session.session_id)] == ["first"]


def test_the_transcript_keeps_insertion_order_not_clock_order(stack: Stack) -> None:
    """Two events in the same millisecond still have an order, because the id has one."""
    session = stack.resolver.start(user_id="u-1", tenant=TENANT)
    bodies = [f"turn-{i}" for i in range(10)]
    for body in bodies:
        stack.transcripts.append(session_id=session.session_id, kind="user", body=body)

    assert [e.body for e in stack.transcripts.for_session(session.session_id)] == bodies


def test_the_turn_index_counts_the_persisted_transcript(stack: Stack) -> None:
    session = stack.resolver.start(user_id="u-1", tenant=TENANT)
    for _ in range(3):
        stack.transcripts.append(session_id=session.session_id, kind="user", body="x")

    view = stack.resolver.resolve(session_id=session.session_id, user_id="u-1", tenant=TENANT)

    assert view.turn_index == 3


def test_working_state_round_trips_and_the_latest_write_wins(
    stack: Stack, app_database: Database
) -> None:
    session = stack.resolver.start(user_id="u-1", tenant=TENANT)
    stack.working_state.put(session.session_id, "draft", {"amount_cents": 1999})
    stack.working_state.put(session.session_id, "draft", {"amount_cents": 2500})
    stack.working_state.put(session.session_id, "step", "prepared")

    restarted = build_stack(app_database, tenant=TENANT)

    assert restarted.working_state.get(session.session_id) == {
        "draft": {"amount_cents": 2500},
        "step": "prepared",
    }


def test_two_sessions_of_one_user_share_no_transcript_and_no_scratchpad(stack: Stack) -> None:
    """The reason the session key is not the user id, stated as a test."""
    a = stack.resolver.start(user_id="u-1", tenant=TENANT)
    b = stack.resolver.start(user_id="u-1", tenant=TENANT)

    stack.transcripts.append(session_id=a.session_id, kind="user", body="about the refund")
    stack.working_state.put(a.session_id, "draft", {"charge_id": "ch-7"})

    assert stack.transcripts.for_session(b.session_id) == ()
    assert stack.working_state.get(b.session_id) == {}
