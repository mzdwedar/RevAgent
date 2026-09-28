"""Three stores, named, and now durable (Part 3).

The session record says who owns a piece of work. The transcript is the authoritative
record of what occurred. The working state is the mutable scratchpad for the live
interaction. Memory is none of these and lives in `agentstack.context.memory`.

They are three tables rather than one with a `kind` column, for the same reason they
are three classes: the moment they share a row shape, someone starts deriving one from
another, and the transcript quietly becomes the context window.

There is no in-memory variant. A fake here would be a second implementation of the
only thing these tests exist to prove - that the state survives the process - and it
would be the one the suite actually exercised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agentstack.control_plane.session import Session
from agentstack.storage.database import Database


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    session_id: str
    kind: str
    body: str
    at: datetime


@dataclass(frozen=True, slots=True)
class SessionStore:
    """The canonical session records. The model never sees one of these."""

    db: Database

    def put(self, session: Session) -> Session:
        self.db.execute(
            "INSERT INTO sessions (session_id, user_id, tenant, created_at)"
            " VALUES (%s, %s, %s, %s)",
            (session.session_id, session.user_id, session.tenant, session.created_at),
        )
        return session

    def get(self, session_id: str) -> Session | None:
        row = self.db.fetch_one(
            "SELECT session_id, user_id, tenant, created_at FROM sessions WHERE session_id = %s",
            (session_id,),
        )
        return None if row is None else Session(*row)


@dataclass(frozen=True, slots=True)
class TranscriptStore:
    """Append-only. Prompt context is derived from it, never equal to it."""

    db: Database

    def append(self, *, session_id: str, kind: str, body: str) -> TranscriptEvent:
        row = self.db.fetch_one(
            "INSERT INTO transcript_events (session_id, kind, body)"
            " VALUES (%s, %s, %s) RETURNING session_id, kind, body, at",
            (session_id, kind, body),
        )
        assert row is not None  # RETURNING on a successful insert always yields a row
        return TranscriptEvent(*row)

    def for_session(self, session_id: str) -> tuple[TranscriptEvent, ...]:
        rows = self.db.fetch_all(
            "SELECT session_id, kind, body, at FROM transcript_events"
            " WHERE session_id = %s ORDER BY id",
            (session_id,),
        )
        return tuple(TranscriptEvent(*row) for row in rows)

    def recent(self, session_id: str, *, kind: str, limit: int) -> tuple[TranscriptEvent, ...]:
        """The last `limit` events of one kind, oldest first. Bounded here, in the query,
        so a long session never loads its whole transcript to hand a turn five items."""
        rows = self.db.fetch_all(
            "SELECT session_id, kind, body, at FROM ("
            "  SELECT id, session_id, kind, body, at FROM transcript_events"
            "  WHERE session_id = %s AND kind = %s ORDER BY id DESC LIMIT %s"
            ") latest ORDER BY id",
            (session_id, kind, limit),
        )
        return tuple(TranscriptEvent(*row) for row in rows)

    def count_for(self, session_id: str) -> int:
        """The turn index, without loading a transcript to measure its length."""
        row = self.db.fetch_one(
            "SELECT count(*) FROM transcript_events WHERE session_id = %s", (session_id,)
        )
        return 0 if row is None else int(row[0])


@dataclass(frozen=True, slots=True)
class WorkingStateStore:
    """Rewindable scratch state. Never the place to record a side effect."""

    db: Database

    def get(self, session_id: str) -> dict[str, Any]:
        rows = self.db.fetch_all(
            "SELECT key, value FROM working_state WHERE session_id = %s ORDER BY key",
            (session_id,),
        )
        return {key: value for key, value in rows}

    def put(self, session_id: str, key: str, value: Any) -> None:
        # Serialised here rather than handed to the driver as a dict: a psycopg Json
        # wrapper in this signature would put a driver type in layer 2 (contract 5).
        self.db.execute(
            "INSERT INTO working_state (session_id, key, value) VALUES (%s, %s, %s::jsonb)"
            " ON CONFLICT (session_id, key)"
            " DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (session_id, key, json.dumps(value)),
        )
