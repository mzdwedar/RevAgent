"""What the drafting turn is told, built from the cycle's record (T40).

The cycle froze a cohort and named it with an `experiment_version`. Drafting is the
model's job: it writes the hypothesis and the variant. Every other argument is taken
from the record, so the model is told them rather than asked to choose them.

Ids only, for now (decided at T40). The cohort's evidence - its size, risk threshold
and value at risk - is not stored when a cycle settles, and giving it to the model is
its own layer-5 task: stored, then assembled into context with provenance and trust.

Built inside the turn activity and never passed through the workflow, so it is never
written into Temporal's history (SPEC-durable-runtime: no prompt in history).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentstack.context.frozen_cohorts import FrozenCohort
from agentstack.observability.audit import AuditSink
from agentstack.runtime.cycles import Cycle
from agentstack.runtime.run import Run
from agentstack.tools.action import ActionRequest
from agentstack.tools.experiments import prepare_rollout

# SPEC.md: "On approval, roll the winning variant out to ~10% of the targeted cohort."
ROLLOUT_PERCENTAGE = 10

# What the audit trail names when a proposal is refused for not being the rollout the
# record says. Refused before anyone is asked, and again at the ask and at the act.
PROPOSAL_DEVIATES = "proposal.deviates"

# Admits or refuses one prepared request before it goes anywhere near the gateway: the
# refusal's reason, or None. A turn that is told exactly what to propose is given one.
Admission = Callable[[ActionRequest], str | None]


class NothingToDraft(RuntimeError):
    """The cycle did not propose, so there is no candidate to draft."""


def estimated_customers(cohort: FrozenCohort, percentage: int = ROLLOUT_PERCENTAGE) -> int:
    """How many customers the rollout reaches: what the approver decides about."""
    return round(cohort.size * percentage / 100)


def intended_rollout(cohort: FrozenCohort, *, prior_rollout_event: int) -> dict[str, Any]:
    """The rollout the record says this cohort gets, as the tool's arguments.

    Every value is the record's: the frozen cohort's own identity and predicate, the
    percentage SPEC.md sets, and the registry's own compare-and-set key for its next
    rollout event (C2), read by the caller so this stays a function of the record and
    not an importer of the execution surface it reads. Nothing here was chosen by a
    model, so it is what a proposal is held to, and what the instruction tells the
    model to propose.
    """
    return {
        "tenant": cohort.tenant,
        "experiment_id": cohort.experiment_id,
        "experiment_version": cohort.experiment_version,
        "percentage": ROLLOUT_PERCENTAGE,
        "targeting_model_version": cohort.targeting_model_version,
        "risk_threshold": cohort.risk_threshold,
        "prior_rollout_event": prior_rollout_event,
    }


def rollout_deviation(
    request: ActionRequest, cohort: FrozenCohort, *, prior_rollout_event: int
) -> str | None:
    """Why `request` is not the rollout the frozen cohort gets, or None if it is.

    The model is told the arguments; it is not trusted to have used them. Its proposal
    is untrusted output, and the approver's headline, the approval's fingerprint and the
    act all rest on it (T43: the proposal is what commits). So a proposal that differs
    from the record in anything - a wider percentage, a looser threshold, another
    cohort's version - is refused here, before a person is asked about it, rather than
    headlined as the rollout they would expect.
    """
    intended = prepare_rollout(intended_rollout(cohort, prior_rollout_event=prior_rollout_event))
    if request.fingerprint() == intended.fingerprint():
        return None
    differs = sorted(
        key
        for key in set(intended.payload) | set(request.payload)
        if request.payload.get(key) != intended.payload.get(key)
    )
    if request.resource != intended.resource:
        differs.insert(0, "resource")
    if request.tool != intended.tool:
        differs.insert(0, "tool")
    return (
        f"{request.tool} on {request.resource} is not the rollout frozen cohort "
        f"{cohort.experiment_version} gets: it differs in {', '.join(differs)}. A proposal "
        "is held to the record, and one that differs is never put to a person"
    )


def rollout_admission(
    *, cohort: FrozenCohort, audit: AuditSink, run: Run, principal: str, prior_rollout_event: int
) -> Admission:
    """An `Admission` for a rollout turn: the frozen cohort's rollout, or a refusal that is
    written to the audit trail, because a proposal stopped before anyone saw it is exactly
    what someone will later ask about."""

    def admit(request: ActionRequest) -> str | None:
        reason = rollout_deviation(request, cohort, prior_rollout_event=prior_rollout_event)
        if reason is not None:
            audit.write(
                run_id=run.run_id,
                principal=principal,
                tenant=run.tenant,
                action_fingerprint=request.fingerprint(),
                surface=request.surface.value,
                resource=request.resource,
                policy_decision=PROPOSAL_DEVIATES,
                approval_id=None,
                outcome="refused",
            )
        return reason

    return admit


def rollout_instruction(
    *, experiment_id: str, cohort: FrozenCohort, prior_rollout_event: int
) -> str:
    """The rollout turn is told the action exactly. Every argument comes from the record:
    the frozen cohort's predicate travels in the payload, because a rollout is to *this*
    cohort. The model proposes it; layer 8 decides it, with a person in the loop."""
    told = intended_rollout(cohort, prior_rollout_event=prior_rollout_event) | {
        "experiment_id": experiment_id
    }
    return (
        "The experiment is drafted. Propose its rollout by calling "
        f"roll_out_variant_to_percentage with tenant={told['tenant']} "
        f"experiment_id={told['experiment_id']} "
        f"experiment_version={told['experiment_version']} "
        f"percentage={told['percentage']} "
        f"targeting_model_version={told['targeting_model_version']} "
        f"risk_threshold={told['risk_threshold']!r} "
        f"prior_rollout_event={told['prior_rollout_event']} exactly as given. A person "
        "approves it before it happens."
    )


def draft_instruction(*, tenant: str, cycle: Cycle) -> str:
    if cycle.outcome is None or cycle.outcome.value != "propose" or cycle.run_id is None:
        raise NothingToDraft(
            f"{cycle.experiment_id} @ {cycle.data_as_of} concluded {cycle.outcome}; only a "
            "cycle that proposed has a frozen cohort to draft an experiment about"
        )
    return (
        "A cohort was frozen for a retention experiment. Draft it by calling "
        f"create_experiment_draft with tenant={tenant} experiment_id={cycle.experiment_id} "
        f"experiment_version={cycle.run_id} exactly as given. Write the hypothesis and the "
        "variant yourself: what offer to test, and why it should retain these customers. "
        f"The cohort was scored on the batch {cycle.data_as_of}."
    )
