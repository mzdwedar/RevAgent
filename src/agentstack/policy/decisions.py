"""Authorization: who may do what, to which resource, in what context.

Note what `decide` does *not* take: model output, tool output, or retrieved text.
Untrusted content must never increase the authority available to a run, and the
cheapest way to guarantee that is to give the authority decision no way to see it
(Identity, trust, policy, approvals, the confused deputy).
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
    # Also in `bound_to`, which the gateway runs first. Held here too because `decide` is
    # the authority decision any caller can ask, and "a halt that names anything but zero
    # is denied by `decide`" is a written bar. Declarative now: the spec says which
    # values it fixes, and no `spec.name ==` branch lives in generic policy.
    if refusal := fixed_payload_refusal(spec, request):
        return refusal
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


def bound_to(spec: ToolSpec, request: ActionRequest, *, verb: str | None) -> Decision | None:
    """Is `request` an instance of `spec` at all? A refusal, or None when it is.

    `decide` judges the spec: its identity, its scope, its tenant. That judgement is
    worth nothing if the request it lets through is some other act - a request built
    as a rollout, handed to the gateway beside the abstention spec, passed every check
    `decide` knows how to make, under a scope that annotates, on a tier no human sees.
    So before anything asks whether the spec is permitted, this asks whether the request
    is the spec's: its tool, its surface, the verb its resource names, and every payload
    value the spec fixes.

    `verb` is what the execution surface reads the resource as, supplied by the gateway
    because only layer 7 knows how its surfaces parse a resource. It is compared, never
    interpreted, here.

    It can only refuse. Reading the payload - model-shaped data - is safe here for that
    reason: nothing in it can make a request *more* permitted, only fail to be the spec's.

    It runs before approval as well as before `decide`, for the reason `halt_rollout`
    first needed it: a rule no approval may override cannot live where an approval is
    consulted. A halt to 5% that a distracted human approved is still not a halt.
    """
    if request.tool != spec.name:
        return Decision(
            False,
            "binding.tool",
            f"the request was prepared by {request.tool!r} and presented under {spec.name!r}; "
            "a spec authorises its own tool's requests and no other",
        )
    if request.surface is not spec.surface:
        return Decision(
            False,
            "binding.surface",
            f"{spec.name} acts on {spec.surface.value}; this request names {request.surface.value}",
        )
    if spec.verb is not None and verb != spec.verb:
        return Decision(
            False,
            "binding.verb",
            f"{spec.name} is a {spec.verb}; its resource {request.resource} names "
            f"{verb or 'nothing the surface serves'}",
        )
    return fixed_payload_refusal(spec, request)


def fixed_payload_refusal(spec: ToolSpec, request: ActionRequest) -> Decision | None:
    """Refuse a request that varies a value its spec fixes - `halt_rollout`'s zero."""
    for key, fixed in spec.fixed_payload.items():
        actual = request.payload.get(key)
        # `type(...) is` as well as `==`: `False == 0` and `0.0 == 0` in Python, and a
        # fixed value that a boolean satisfies is not fixed.
        if type(actual) is not type(fixed.value) or actual != fixed.value:
            return Decision(
                False,
                "binding.payload",
                f"{spec.name} fixes {key} at {fixed.value!r} and this request names "
                f"{actual!r}: {fixed.because}",
            )
    return None
