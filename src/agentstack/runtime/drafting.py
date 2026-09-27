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

from agentstack.context.frozen_cohorts import FrozenCohort
from agentstack.runtime.cycles import Cycle

# SPEC.md: "On approval, roll the winning variant out to ~10% of the targeted cohort."
ROLLOUT_PERCENTAGE = 10


class NothingToDraft(RuntimeError):
    """The cycle did not propose, so there is no candidate to draft."""


def estimated_customers(cohort: FrozenCohort, percentage: int = ROLLOUT_PERCENTAGE) -> int:
    """How many customers the rollout reaches: what the approver decides about."""
    return round(cohort.size * percentage / 100)


def rollout_instruction(*, experiment_id: str, cohort: FrozenCohort) -> str:
    """The rollout turn is told the action exactly. Every argument comes from the record:
    the frozen cohort's predicate travels in the payload, because a rollout is to *this*
    cohort. The model proposes it; layer 8 decides it, with a person in the loop."""
    return (
        "The experiment is drafted. Propose its rollout by calling "
        f"roll_out_variant_to_percentage with tenant={cohort.tenant} "
        f"experiment_id={experiment_id} experiment_version={cohort.experiment_version} "
        f"percentage={ROLLOUT_PERCENTAGE} "
        f"targeting_model_version={cohort.targeting_model_version} "
        f"risk_threshold={cohort.risk_threshold!r} exactly as given. A person approves it "
        "before it happens."
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
