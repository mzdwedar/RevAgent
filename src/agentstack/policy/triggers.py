"""Looking is not deciding (Identity, trust, policy, approvals authority, applied to triggers).

The two trigger kinds wake the same run and do not carry the same authority, and that
asymmetry is the load-bearing decision in the spec.

Waking on `metric_movement` means evaluating **precisely when noise is largest**. If
that wake could also propose a rollout, the system would be an optional-stopping
machine: structurally biased toward acting on the looks that flatter the variant. A
`data_arrival` wake has no such bias, because a watermark advancing is independent of
what the data says.

So a `metric_movement` can stop an experiment and can never advance one. That is not a
compromise. Stopping early for harm does not need protection against false positives in
the way stopping early for benefit does - you do not owe statistical rigour to the claim
"this is hurting people, stop".

The trigger decides **when we look**. The inference rule decides **when we may decide**.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TriggerKind(StrEnum):
    DATA_ARRIVAL = "data_arrival"
    METRIC_MOVEMENT = "metric_movement"


class Outcome(StrEnum):
    """What one evaluation cycle concluded."""

    # Evidence refreshed, nothing else changed. Both kinds may reach this.
    CONTINUE = "continue"
    # A guardrail tripped: stop the experiment. Both kinds may reach this.
    ABSTAIN = "abstain"
    # An analysis point was reached: propose a rollout. `data_arrival` only.
    PROPOSE = "propose"


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """A well-formed trigger. Nothing here has been authorised yet.

    It lives beside the authority table rather than in `agentstack.interfaces`, where
    the parsing is: the runtime has to consume one, and contract 4 says nothing imports
    the channel layer. The kind is what carries authority, so the event belongs with
    the rule that reads it.
    """

    kind: TriggerKind
    experiment_id: str
    data_as_of: str
    # A claim about who this is for, not an authorisation. Layer 8 tests it.
    tenant: str
    # Where it came from, for the audit trail. A string, not an import.
    source: str = "unknown"

    def cycle_key(self) -> str:
        return f"{self.experiment_id}:{self.data_as_of}:{self.kind.value}"


class OutcomeNotAuthorized(PermissionError):
    """This trigger kind may not reach this outcome."""


# Which outcomes each kind may reach. Written as data so the asymmetry can be read
# rather than traced through branches - it is the design, not an implementation detail.
AUTHORITY: dict[TriggerKind, frozenset[Outcome]] = {
    TriggerKind.DATA_ARRIVAL: frozenset(Outcome),
    TriggerKind.METRIC_MOVEMENT: frozenset({Outcome.CONTINUE, Outcome.ABSTAIN}),
}


def may_reach(kind: TriggerKind, outcome: Outcome) -> bool:
    return outcome in AUTHORITY[kind]


def authorize(kind: TriggerKind, outcome: Outcome) -> Outcome:
    """Check an outcome against the kind that produced it, before it is recorded.

    Checked at the point of recording rather than where the outcome is computed,
    because the evaluator is the thing that might be wrong. A rule enforced only inside
    the code it constrains is a comment.
    """
    if not may_reach(kind, outcome):
        raise OutcomeNotAuthorized(
            f"a {kind.value} trigger reached {outcome.value}, which it may never do: "
            "waking on a metric crossing a threshold means looking precisely when "
            "noise is largest, and a look that can also advance an experiment is an "
            "optional-stopping machine"
        )
    return outcome


class TenantClaimRefused(PermissionError):
    """A trigger claimed a tenant other than the one its run acts for."""


def authorize_trigger_tenant(trigger: TriggerEvent, *, run_tenant: str) -> TriggerEvent:
    """Test a trigger's tenant claim against the tenant the run acts for.

    `run_tenant` comes from the run's own record, never from the payload the claim
    arrived in (the same rule `authorize_approver` keeps for an approver). Whether a
    *source* may speak for a tenant at all is not decided here: nothing yet records which
    sources belong to which tenant (SPEC, Layer 8), so this refuses a claim that crosses
    runs and does not pretend to authenticate the sender.
    """
    if trigger.tenant != run_tenant:
        raise TenantClaimRefused(
            f"a trigger for tenant {trigger.tenant!r} cannot wake a run acting for {run_tenant!r}"
        )
    return trigger


def subject_of(trigger: TriggerEvent, *, tenant: str) -> str:
    """The experiment a run woken by this trigger is bound to, once the claim is tested.

    The trigger is the trusted source for *which experiment*: it is what woke the run,
    before any model or tool said anything. Its tenant is only a claim (STACK row 1),
    so the subject is released only against the tenant the run already acts for. The
    run's tenant comes from its session, never from the payload the claim arrived in.
    """
    return authorize_trigger_tenant(trigger, run_tenant=tenant).experiment_id
