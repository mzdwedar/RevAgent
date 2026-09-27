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
    if refusal := halt_only_zeroes(spec, request):
        return Decision(False, "halt.only_zeroes", refusal)
    return Decision(True, "allow", f"{envelope.principal} may {spec.name} on {request.resource}")


def halt_only_zeroes(spec: ToolSpec, request: ActionRequest) -> str | None:
    """A halt sets exposure to zero, and nothing else.

    `halt_rollout` has no percentage argument, so its own prepare cannot name another
    number. This holds for a request built any other way. It is here, not in the
    `PRE_COMMIT` rule set, because there a refusal only escalates to a human, and an
    existing grant skips the rules altogether. A halt to 5% is a rollout wearing a
    halt's name, and no approval makes it one.
    """
    if spec.name != "halt_rollout" or request.payload.get("percentage") == 0:
        return None
    return (
        f"a halt sets exposure to zero; this one names {request.payload.get('percentage')!r}, "
        "which only a rollout, with a human's approval, may do"
    )
