"""The identity envelope every side-effecting action carries (Part 7).

"Run it as the service account" is how a planning error or an injected instruction
becomes a service-level incident. An envelope forces the questions to be answered per
action: who is acting, under what delegation, against which tenant, with which
credential, for how long, and can it be revoked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
        # Which one resource this run may change, or None for a run bound to none.
        # Declared on every envelope so "is this narrowed?" is always answerable.
        "subject",
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
    # The resource root this run is about, e.g. `acme/experiments/exp-7`. Set, it caps
    # every side effect to that resource and what hangs under it (`decide`, rule
    # `subject.boundary`). A scope says *what kind* of act; the subject says *to what*.
    # Without it, `experiments:halt` is authority over every experiment in the tenant,
    # and the run that holds it can be pointed at any of them by text it read (H3).
    subject: str | None = None

    def __post_init__(self) -> None:
        if not self.principal or not self.tenant or not self.credential_ref:
            raise ValueError("an identity envelope needs a principal, a tenant and a credential")
        if not self.delegation_scopes:
            raise ValueError("an envelope with no delegation scopes is a god token by another name")
        if self.subject is not None and not self.subject.startswith(f"{self.tenant}/"):
            raise ValueError(f"subject {self.subject} is outside tenant {self.tenant}")

    def bound_to(self, subject: str) -> IdentityEnvelope:
        """The same envelope, narrowed to one resource. Narrowing only: an envelope
        already bound elsewhere is refused rather than re-pointed, because re-pointing
        is widening with extra steps."""
        if self.subject is not None and self.subject != subject:
            raise ValueError(
                f"this envelope is bound to {self.subject}; it cannot be re-bound to {subject}"
            )
        return replace(self, subject=subject)

    def within_subject(self, resource: str) -> bool:
        """The resource is the subject, or under it. A path segment, not a prefix:
        `exp-7` must not cover `exp-70`."""
        if self.subject is None:
            return True
        return resource == self.subject or resource.startswith(f"{self.subject}/")

    def is_live(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) < self.expires_at

    def grants(self, scope: str) -> bool:
        """Exact scope match. Widening is a decision someone makes, not a prefix trick."""
        return scope in self.delegation_scopes
