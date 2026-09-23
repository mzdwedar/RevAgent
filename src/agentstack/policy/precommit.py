"""What permits a `PRE_COMMIT` action when no human is woken (Part 7).

`PRE_COMMIT` means a rule decides. That is only worth having if the rule can say no -
a policy that always permits is a tier with a nicer name, which is what this tier was
before T11.

So the default is **refuse**. An action reaches a rule that permits it, or it does not
proceed. Adding a tier that can only ever say yes would have been the same defect as
ADR-0004's dead containment dimensions: something declared, nothing enforced.

Checks are conjunctive. Every one must be satisfied, because a first-match rule set
lets a permissive rule shadow a restrictive one that was added later precisely to
catch the case the permissive one waves through.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from agentstack.policy.envelope import IdentityEnvelope
from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import ToolSpec

# A check returns a refusal reason, or None when it is satisfied.
Check = Callable[[ToolSpec, ActionRequest, IdentityEnvelope], str | None]


@dataclass(frozen=True, slots=True)
class PolicyGrant:
    """A permission minted by a rule rather than by a person."""

    rule: str
    reason: str


def reversible_only(
    spec: ToolSpec, request: ActionRequest, envelope: IdentityEnvelope
) -> str | None:
    """Nothing irreversible proceeds without a human.

    `ToolSpec` already refuses to register an irreversible tool at any tier but
    `ALWAYS`, so this should be unreachable. It is here because "should be unreachable"
    is how the previous version of this tier was described too, and the cost of the
    check is one comparison against an action that cannot be undone.
    """
    del request, envelope
    if not spec.reversible:
        return f"{spec.name} is irreversible; no rule may permit it without a human"
    return None


def within_acting_tenant(
    spec: ToolSpec, request: ActionRequest, envelope: IdentityEnvelope
) -> str | None:
    """The resource belongs to the tenant the envelope acts for.

    Containment checks this too, later. Checking it here as well means a cross-tenant
    action is refused *by policy*, and the audit trail says so - "containment stopped
    it" and "policy never permitted it" are different answers to how it was caught.
    """
    del spec
    if not request.resource.startswith(f"{envelope.tenant}/"):
        return (
            f"{request.resource} is not within tenant {envelope.tenant}; a rule does not "
            "get to permit an action across a tenant boundary"
        )
    return None


DEFAULT_CHECKS: tuple[Check, ...] = (reversible_only, within_acting_tenant)


@dataclass(frozen=True, slots=True)
class PreCommitPolicy:
    """The named rule that PRE_COMMIT actions are judged against."""

    name: str = "pre_commit.reversible_within_tenant"
    checks: tuple[Check, ...] = field(default=DEFAULT_CHECKS)

    def permit(
        self, *, spec: ToolSpec, request: ActionRequest, envelope: IdentityEnvelope
    ) -> PolicyGrant | None:
        """A grant, or None with nothing implied about why.

        Returning None rather than raising, because the caller decides what a refusal
        means: for `PRE_COMMIT` it is "ask a human after all" in a later iteration, and
        for now it is a refusal the audit trail records.
        """
        if not self.checks:
            # An empty rule set permits everything, which is the failure this default
            # exists to prevent. Say so rather than silently granting.
            return None
        for check in self.checks:
            if check(spec, request, envelope) is not None:
                return None
        return PolicyGrant(
            rule=self.name,
            reason=f"{spec.name} is reversible and within {envelope.tenant}",
        )

    def refusals(
        self, *, spec: ToolSpec, request: ActionRequest, envelope: IdentityEnvelope
    ) -> tuple[str, ...]:
        """Why it was refused, for the audit trail and for a human reading it later."""
        if not self.checks:
            return ("the policy has no checks, so it permits nothing",)
        return tuple(
            reason
            for check in self.checks
            if (reason := check(spec, request, envelope)) is not None
        )
