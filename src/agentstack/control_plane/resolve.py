"""Resolving an inbound event to the session that owns it.

Session ownership answers "which record do I load". Authorization answers "what is
this run allowed to do", and lives in layer 8. Collapsing the two is how a resumed
session silently keeps permissions it should have lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentstack.control_plane.session import Session, SessionView, new_session
from agentstack.control_plane.stores import TranscriptStore, WorkingStateStore


class SessionOwnershipError(RuntimeError):
    """The event cannot be resolved to exactly one owning session."""


@dataclass(slots=True)
class SessionResolver:
    transcripts: TranscriptStore
    working_state: WorkingStateStore
    _sessions: dict[str, Session] = field(default_factory=dict)

    def start(self, *, user_id: str, tenant: str) -> Session:
        session = new_session(user_id=user_id, tenant=tenant)
        self._sessions[session.session_id] = session
        return session

    def resolve(
        self, *, session_id: str, user_id: str, tenant: str, stage: str = "default"
    ) -> SessionView:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionOwnershipError(f"{session_id} is not a known session")
        if session.user_id != user_id or session.tenant != tenant:
            raise SessionOwnershipError(
                "the caller does not own this session; continuity is not authorization"
            )
        return SessionView(
            session_id=session.session_id,
            user_id=session.user_id,
            tenant=session.tenant,
            stage=stage,
            turn_index=len(self.transcripts.for_session(session_id)),
        )
