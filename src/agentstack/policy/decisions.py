"""Authorization: who may do what, to which resource, in what context.

Note what `decide` does *not* take: model output, tool output, or retrieved text.
Untrusted content must never increase the authority available to a run, and the
cheapest way to guarantee that is to give the authority decision no way to see it
(Part 7, the confused deputy).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agentstack.policy.envelope import IdentityEnvelope
from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import ToolSpec


class PolicyDenied(PermissionError):
    """Raised when policy refuses an action. Not a model problem."""


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    rule: str
    reason: str


def decide(
    *,
    envelope: IdentityEnvelope,
    spec: ToolSpec,
    request: ActionRequest,
    now: datetime | None = None,
) -> Decision:
    if not envelope.is_live(now):
        return Decision(False, "envelope.expired", "credential lifetime has elapsed")
    if envelope.acts_as is not spec.acts_as:
        return Decision(
            False,
            "identity.mismatch",
            f"tool acts as {spec.acts_as.value}, envelope acts as {envelope.acts_as.value}",
        )
    if not envelope.grants(spec.scope):
        return Decision(False, "scope.missing", f"envelope does not carry the {spec.scope} scope")
    if not request.resource.startswith(f"{envelope.tenant}/"):
        return Decision(
            False,
            "tenant.boundary",
            f"resource {request.resource} is outside tenant {envelope.tenant}",
        )
    return Decision(True, "allow", f"{envelope.principal} may {spec.name} on {request.resource}")
