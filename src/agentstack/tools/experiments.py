"""The Experiment Operator's own capabilities (Part 6).

Two tools, and the difference between them is the whole design:

* `create_experiment_draft` writes a candidate into the registry. Reversible - a draft
  is deleted - so it is `PRE_COMMIT`: a rule decides, nobody is woken.
* `roll_out_variant_to_percentage` puts a variant in front of real customers.
  Irreversible, so `ALWAYS`: a person, every time.

Each lives on its own stage. A run drafting an experiment is never shown the rollout
tool, and a refund run is shown neither - which is the exposure filter doing its actual
job rather than being a field nobody reads. Exposing both to every run would hand a
prompt injection a menu with the irreversible item on it. (They once shared a single
`experiment` stage, which is exactly that menu; `migrations/0011` moved those runs.)

**The rollout carries the cohort predicate.** `targeting_model_version` and
`risk_threshold` are required arguments, not context the tool looks up for itself: an
approval is granted against a specific frozen cohort, and a rollout that re-derived the
cohort at execution time could roll out to a different population than the one a human
saw (criterion 14).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import ActsAs, Approval, Idempotency, Surface, ToolSpec

# One stage per half of the lifecycle, so the exposure filter separates them. A drafting
# turn sees drafting tools, an evaluation turn sees what may stop an experiment, and a
# rollout turn - the one that follows a human's yes - sees the rollout and nothing else.
DRAFT_STAGE = "draft"
EVALUATION_STAGE = "evaluation"
ROLLOUT_STAGE = "rollout"

DRAFT = ToolSpec(
    name="create_experiment_draft",
    description=(
        "Write a candidate experiment into the registry as a draft. Drafts are not "
        "shown to any customer and can be deleted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": {"type": "string", "format": "id"},
            "experiment_id": {"type": "string", "format": "id"},
            "experiment_version": {"type": "string", "format": "id"},
            "hypothesis": {"type": "string"},
            "variant": {"type": "string"},
        },
        "required": [
            "tenant",
            "experiment_id",
            "experiment_version",
            "hypothesis",
            "variant",
        ],
    },
    acts_as=ActsAs.DELEGATED,
    scope="experiments:write",
    surface=Surface.REGISTRY,
    side_effecting=True,
    reversible=True,
    approval=Approval.PRE_COMMIT,
    idempotency=Idempotency.KEY,
    stages=frozenset({DRAFT_STAGE}),
)

ROLLOUT = ToolSpec(
    name="roll_out_variant_to_percentage",
    description=(
        "Expose a variant to a percentage of the targeted cohort. Real customers see the change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": {"type": "string", "format": "id"},
            "experiment_id": {"type": "string", "format": "id"},
            "experiment_version": {"type": "string", "format": "id"},
            "percentage": {"type": "integer"},
            # The cohort predicate. Required, so a rollout cannot be prepared without
            # naming the population an approval was granted against.
            "targeting_model_version": {"type": "string", "format": "id"},
            "risk_threshold": {"type": "number"},
        },
        "required": [
            "tenant",
            "experiment_id",
            "experiment_version",
            "percentage",
            "targeting_model_version",
            "risk_threshold",
        ],
    },
    acts_as=ActsAs.DELEGATED,
    scope="experiments:rollout",
    surface=Surface.REGISTRY,
    side_effecting=True,
    reversible=False,
    approval=Approval.ALWAYS,
    idempotency=Idempotency.KEY,
    reversal_note=(
        "cannot be undone: customers who saw the variant saw it. Reversing means "
        "rolling the percentage back to zero, which stops future exposure and does "
        "not unsee the offer or refund what it cost"
    ),
    stages=frozenset({ROLLOUT_STAGE}),
)


def prepare_draft(arguments: Mapping[str, Any]) -> ActionRequest:
    """Arguments arrive validated against `input_schema`, so these are declared types."""
    tenant = arguments["tenant"]
    experiment = arguments["experiment_id"]
    version = arguments["experiment_version"]
    return ActionRequest(
        tool=DRAFT.name,
        surface=DRAFT.surface,
        resource=f"{tenant}/experiments/{experiment}",
        payload={
            "experiment_version": version,
            "hypothesis": arguments["hypothesis"],
            "variant": arguments["variant"],
        },
        # Keyed on the frozen version, not on a uuid. Drafting the same candidate for
        # the same frozen cohort twice is one draft; drafting against a different
        # cohort is a different candidate and must not deduplicate into the first.
        idempotency_key=f"draft:{tenant}:{experiment}:{version}",
    )


def prepare_rollout(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    experiment = arguments["experiment_id"]
    version = arguments["experiment_version"]
    percentage = arguments["percentage"]
    return ActionRequest(
        tool=ROLLOUT.name,
        surface=ROLLOUT.surface,
        resource=f"{tenant}/experiments/{experiment}/rollout",
        payload={
            "experiment_version": version,
            "percentage": percentage,
            "targeting_model_version": arguments["targeting_model_version"],
            "risk_threshold": arguments["risk_threshold"],
        },
        # The percentage is in the key: rolling out to 10% and later to 25% are two
        # different effects, and the second must not be swallowed as a retry of the
        # first. The version is in it for the same reason as the draft.
        idempotency_key=f"rollout:{tenant}:{experiment}:{version}:{percentage}",
    )
