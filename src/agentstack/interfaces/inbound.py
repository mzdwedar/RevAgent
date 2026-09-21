"""The event a channel hands to the control plane."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InboundEvent:
    channel: str
    tenant: str
    user_id: str
    session_id: str
    text: str
