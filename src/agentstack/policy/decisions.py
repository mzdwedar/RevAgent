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
    if spec.side_effecting and not envelope.within_subject(request.resource):
        return Decision(False, "subject.boundary", outside_subject(envelope, request))
    if refusal := halt_only_zeroes(spec, request):
        return Decision(False, "halt.only_zeroes", refusal)
    return Decision(True, "allow", f"{envelope.principal} may {spec.name} on {request.resource}")


def outside_subject(envelope: IdentityEnvelope, request: ActionRequest) -> str:
    """Why a run bound to one resource may not change another (H3).

    An evaluation run is woken about one experiment and holds `experiments:halt`.
    Without this, that scope reached every experiment in the tenant, and the run was a
    confused deputy: text it read - another experiment's hypothesis, written by whoever
    could draft - named a different live experiment, and the halt was granted.
    Here, not in the `PRE_COMMIT` rule set: a refusal there only escalates to a human,
    and an existing grant skips the rules, so a person approving the wrong
    experiment's halt would get it through. `decide` runs first, on every call.

    Writes only. A read changes nothing, and what it returns reaches a later turn as
    untrusted data that - with every write bounded here - can steer the run only
    towards its own subject. An evaluation also has a real use for the neighbours:
    `list_experiments` is on its menu, and its resource is the collection, which no
    one experiment contains. Bounding reads would deny that tool on every call and
    buy nothing the write boundary does not already hold.
    """
    return (
        f"this run is bound to {envelope.subject}; {request.resource} is another resource, "
        "and what a run read does not choose what it may change"
    )


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
