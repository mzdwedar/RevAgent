"""Sessions as isolation boundaries, not as chat history (Part 3).

One user can own many sessions; one session spans many turns. Keying a session on the
user id looks fine right up until two unrelated tasks share a transcript and a
scratchpad, and then it is not fine at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class Session:
    """The canonical record. The model never sees this object."""

    session_id: str
    user_id: str
    tenant: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.session_id == self.user_id:
            raise ValueError(
                "session_id must not be the user id (Part 3): unrelated tasks would "
                "share transcript and working state"
            )


@dataclass(frozen=True, slots=True)
class SessionView:
    """The bounded view handed to the runtime for one turn."""

    session_id: str
    user_id: str
    tenant: str
    stage: str
    turn_index: int


def new_session(*, user_id: str, tenant: str) -> Session:
    return Session(
        session_id=f"sess-{uuid.uuid4()}",
        user_id=user_id,
        tenant=tenant,
        created_at=datetime.now(UTC),
    )
