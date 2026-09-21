"""Layer 2 - control plane and session ownership.

Takes an inbound event, resolves it to a session, loads the authoritative record and
the working state, and hands the runtime a *bounded view* for this turn. Three things
it keeps apart (Part 3): the transcript (what happened), the working state (the live
scratchpad) and memory (durable state that lives outside the session, in layer 5).
"""

from agentstack.control_plane.resolve import SessionOwnershipError, SessionResolver
from agentstack.control_plane.session import Session, SessionView
from agentstack.control_plane.stores import TranscriptEvent, TranscriptStore, WorkingStateStore

__all__ = [
    "Session",
    "SessionOwnershipError",
    "SessionResolver",
    "SessionView",
    "TranscriptEvent",
    "TranscriptStore",
    "WorkingStateStore",
]
