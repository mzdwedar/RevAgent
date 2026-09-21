"""The identity envelope every side-effecting action carries (Part 7).

"Run it as the service account" is how a planning error or an injected instruction
becomes a service-level incident. An envelope forces the questions to be answered per
action: who is acting, under what delegation, against which tenant, with which
credential, for how long, and can it be revoked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from agentstack.tools.spec import ActsAs

REQUIRED_ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {
        "principal",
        "acts_as",
        "tenant",
        "delegation_scopes",
        "credential_ref",
        "expires_at",
        "revocable",
    }
)


@dataclass(frozen=True, slots=True)
class IdentityEnvelope:
    principal: str
    acts_as: ActsAs
    tenant: str
    delegation_scopes: frozenset[str]
    credential_ref: str
    expires_at: datetime
    revocable: bool

    def __post_init__(self) -> None:
        if not self.principal or not self.tenant or not self.credential_ref:
            raise ValueError("an identity envelope needs a principal, a tenant and a credential")
        if not self.delegation_scopes:
            raise ValueError("an envelope with no delegation scopes is a god token by another name")

    def is_live(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) < self.expires_at

    def grants(self, scope: str) -> bool:
        """Exact scope match. Widening is a decision someone makes, not a prefix trick."""
        return scope in self.delegation_scopes
