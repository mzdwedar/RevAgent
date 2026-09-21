"""What every piece of context has to carry before it is allowed near a prompt.

Part 5: an item without a scope leaks across users or tenants; an item without
provenance and freshness cannot be audited at the failure point; an item without a
trust label invites a confused deputy two layers up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

REQUIRED_ITEM_FIELDS: frozenset[str] = frozenset(
    {"scope", "provenance", "observed_at", "reason", "trust"}
)


class Trust(Enum):
    """Where the bytes came from. Never an authority decision on its own."""

    FIRST_PARTY = "first_party"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True, slots=True)
class Scope:
    """Who this belongs to. Every retrieved or remembered item needs one."""

    tenant: str
    user: str | None = None
    session: str | None = None
    project: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant:
            raise ValueError("scope.tenant is required: an unscoped item leaks across tenants")

    def covers(self, other: Scope) -> bool:
        """True when a run scoped to `self` may see an item scoped to `other`."""
        if other.tenant != self.tenant:
            return False
        if other.user is not None and other.user != self.user:
            return False
        if other.session is not None and other.session != self.session:
            return False
        return not (other.project is not None and other.project != self.project)


@dataclass(frozen=True, slots=True)
class ContextItem:
    kind: str
    text: str
    scope: Scope
    provenance: str
    observed_at: datetime
    reason: str
    trust: Trust

    def __post_init__(self) -> None:
        if not self.provenance:
            raise ValueError(f"{self.kind}: provenance is required (Part 5)")
        if not self.reason:
            raise ValueError(f"{self.kind}: an inclusion reason is required (Part 5)")
