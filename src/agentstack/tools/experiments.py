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

import hashlib
from collections.abc import Mapping
from typing import Any

from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import ActsAs, Approval, Fixed, Idempotency, Surface, ToolSpec

# One stage per half of the lifecycle, so the exposure filter separates them. A drafting
# turn sees drafting tools, an evaluation turn sees what may stop an experiment, and a
# rollout turn - the one that follows a human's yes - sees the rollout and nothing else.
DRAFT_STAGE = "draft"
EVALUATION_STAGE = "evaluation"
ROLLOUT_STAGE = "rollout"
EXPERIMENT_STAGES = frozenset({DRAFT_STAGE, EVALUATION_STAGE, ROLLOUT_STAGE})

# The four states an experiment can be in (migrations/0012). A read may filter by one;
# no tool may name one as a target - a transition is a tool, not an argument.
STATUSES = ["draft", "live", "halted", "discarded"]

# An id becomes one segment of a resource path (`{tenant}/experiments/{experiment}/...`),
# and the registry reads the act from the path's last segment. `format: id` is enforced
# by the validator as a single segment, so `exp-9/rollout` is refused as an argument
# rather than read by the surface as a rollout (C1).
_ID = {"type": "string", "format": "id"}


def experiment_resource(tenant: str, experiment_id: str) -> str:
    """The resource root one experiment's requests live under. Every prepare below
    names this path or one beneath it, which is what lets a run be bound to it."""
    return f"{tenant}/experiments/{experiment_id}"


def _read(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    stages: frozenset[str],
    verb: str,
) -> ToolSpec:
    """A registry read: no effect, no approval, one scope for all of them.

    What it returns includes model-authored prose (hypotheses), so it enters the next
    turn's context as untrusted - the read is safe, its content is not.
    """
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {"tenant": _ID, **properties},
            "required": ["tenant", *required],
        },
        acts_as=ActsAs.DELEGATED,
        scope="experiments:read",
        surface=Surface.REGISTRY,
        side_effecting=False,
        reversible=True,
        approval=Approval.NONE,
        idempotency=Idempotency.NATURAL,
        stages=stages,
        verb=verb,
    )


GET = _read(
    "get_experiment",
    "Read one experiment: its status, current version, variant and latest hypothesis.",
    {"experiment_id": _ID},
    ["experiment_id"],
    EXPERIMENT_STAGES,
    "experiment",
)

LIST = _read(
    "list_experiments",
    "List this tenant's experiments, optionally only those in one status. At most 50.",
    {
        "status": {"type": "string", "enum": STATUSES},
        # Bounded in the schema, so one read cannot flood the context window.
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    [],
    EXPERIMENT_STAGES,
    "list",
)

# Not on the draft stage: a drafting turn has nothing live to look back on, and every
# tool on a stage is one more thing an injection can ask for.
HISTORY = _read(
    "get_rollout_history",
    "Read one experiment's rollouts and halts, oldest first, and its current exposure.",
    {"experiment_id": _ID},
    ["experiment_id"],
    frozenset({EVALUATION_STAGE, ROLLOUT_STAGE}),
    "history",
)

DRAFT = ToolSpec(
    name="create_experiment_draft",
    description=(
        "Write a candidate experiment into the registry as a draft. Drafts are not "
        "shown to any customer and can be deleted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": _ID,
            "experiment_id": _ID,
            "experiment_version": _ID,
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
    scope="experiments:draft",
    surface=Surface.REGISTRY,
    side_effecting=True,
    reversible=True,
    approval=Approval.PRE_COMMIT,
    idempotency=Idempotency.KEY,
    stages=frozenset({DRAFT_STAGE}),
    verb="draft",
)

ROLLOUT = ToolSpec(
    name="roll_out_variant_to_percentage",
    description=(
        "Expose a variant to a percentage of the targeted cohort. Real customers see the change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": _ID,
            "experiment_id": _ID,
            "experiment_version": _ID,
            # Bounded, so 150% is refused as an argument rather than stored as a fact.
            "percentage": {"type": "integer", "minimum": 0, "maximum": 100},
            # The cohort predicate. Required, so a rollout cannot be prepared without
            # naming the population an approval was granted against.
            "targeting_model_version": _ID,
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
    verb="rollout",
)


# --- the draft lifecycle: reword it, or throw it away ---
#
# Both name the version, so a call prepared against one frozen cohort cannot land on
# another, and neither can reach anything but a draft: the store's `WHERE status =
# 'draft'` is on the same statement as the change.

REVISE = ToolSpec(
    name="revise_draft_hypothesis",
    description=(
        "Reword a draft experiment's hypothesis. The variant and the cohort are unchanged; "
        "only drafts can be revised."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": _ID,
            "experiment_id": _ID,
            "experiment_version": _ID,
            "hypothesis": {"type": "string"},
        },
        # No variant: rewording the reason cannot change what customers would see.
        "required": ["tenant", "experiment_id", "experiment_version", "hypothesis"],
    },
    acts_as=ActsAs.DELEGATED,
    scope="experiments:draft",
    surface=Surface.REGISTRY,
    side_effecting=True,
    reversible=True,
    approval=Approval.PRE_COMMIT,
    idempotency=Idempotency.KEY,
    stages=frozenset({DRAFT_STAGE}),
    verb="revise",
)

DISCARD = ToolSpec(
    name="discard_experiment_draft",
    description=(
        "Discard a draft experiment that should not go ahead. Only drafts can be discarded."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": _ID,
            "experiment_id": _ID,
            "experiment_version": _ID,
            "reason": {"type": "string"},
        },
        "required": ["tenant", "experiment_id", "experiment_version", "reason"],
    },
    acts_as=ActsAs.DELEGATED,
    scope="experiments:draft",
    surface=Surface.REGISTRY,
    side_effecting=True,
    reversible=True,
    approval=Approval.PRE_COMMIT,
    idempotency=Idempotency.KEY,
    stages=frozenset({DRAFT_STAGE}),
    verb="discard",
)


# --- the evaluation record ---

ABSTAIN = ToolSpec(
    name="record_abstention",
    description=(
        "Record why this evaluation took no action on an experiment. Changes no status "
        "and exposes nothing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": _ID,
            "experiment_id": _ID,
            "experiment_version": _ID,
            "explanation": {"type": "string"},
        },
        "required": ["tenant", "experiment_id", "experiment_version", "explanation"],
    },
    acts_as=ActsAs.DELEGATED,
    # Its own scope: appending a note is a smaller blast radius than halting, and a
    # grant to annotate must not imply a grant to stop anything.
    scope="experiments:annotate",
    surface=Surface.REGISTRY,
    side_effecting=True,
    reversible=True,
    approval=Approval.PRE_COMMIT,
    idempotency=Idempotency.KEY,
    stages=frozenset({EVALUATION_STAGE}),
    verb="abstention",
)


HALT = ToolSpec(
    name="halt_rollout",
    description=(
        "Stop exposing a live experiment's variant to new customers. Sets exposure to "
        "zero; it cannot set any other percentage."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": _ID,
            "experiment_id": _ID,
            "experiment_version": _ID,
            "reason": {"type": "string"},
        },
        # No percentage: a halt that could name a number is a rollout with a nicer name.
        "required": ["tenant", "experiment_id", "experiment_version", "reason"],
    },
    acts_as=ActsAs.DELEGATED,
    scope="experiments:halt",
    surface=Surface.REGISTRY,
    side_effecting=True,
    reversible=True,
    # Stopping exposure is the safe direction: a guardrail breach must not wait on Slack
    # (assumption 3). A rule decides, and the rule only ever sees zero.
    approval=Approval.PRE_COMMIT,
    idempotency=Idempotency.KEY,
    stages=frozenset({EVALUATION_STAGE}),
    verb="halt",
    fixed_payload={
        "percentage": Fixed(
            0,
            "a halt sets exposure to zero, and only a rollout, with a human's approval, "
            "may name any other percentage",
        )
    },
)


def prepare_halt(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    experiment = arguments["experiment_id"]
    version = arguments["experiment_version"]
    return ActionRequest(
        tool=HALT.name,
        surface=HALT.surface,
        resource=f"{tenant}/experiments/{experiment}/halt",
        # The fixed values come from the spec, so what prepare writes and what the gateway
        # holds a halt to cannot drift apart.
        payload={
            "experiment_version": version,
            **{key: fixed.value for key, fixed in HALT.fixed_payload.items()},
            "reason": arguments["reason"],
        },
        # One halt per version: halting twice is a retry, not a second effect.
        idempotency_key=f"halt:{tenant}:{experiment}:{version}",
    )


def prepare_abstain(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    experiment = arguments["experiment_id"]
    version = arguments["experiment_version"]
    explanation = arguments["explanation"]
    return ActionRequest(
        tool=ABSTAIN.name,
        surface=ABSTAIN.surface,
        resource=f"{tenant}/experiments/{experiment}/abstention",
        payload={"experiment_version": version, "explanation": explanation},
        # One cycle abstaining twice for the same reason is a retry; a later cycle with
        # a different explanation is a second record.
        idempotency_key=f"abstain:{tenant}:{experiment}:{version}:{_digest(explanation)}",
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def prepare_revise(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    experiment = arguments["experiment_id"]
    version = arguments["experiment_version"]
    hypothesis = arguments["hypothesis"]
    return ActionRequest(
        tool=REVISE.name,
        surface=REVISE.surface,
        resource=f"{tenant}/experiments/{experiment}/revision",
        payload={"experiment_version": version, "hypothesis": hypothesis},
        # The wording is the identity: the same words twice is one revision, and new
        # words are a new one. Hashed, so the key stays short whatever the model wrote.
        idempotency_key=f"revise:{tenant}:{experiment}:{version}:{_digest(hypothesis)}",
    )


def prepare_discard(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    experiment = arguments["experiment_id"]
    version = arguments["experiment_version"]
    return ActionRequest(
        tool=DISCARD.name,
        surface=DISCARD.surface,
        resource=f"{tenant}/experiments/{experiment}/discard",
        payload={"experiment_version": version, "reason": arguments["reason"]},
        # One discard per version, whatever the reason says: discarding twice is a
        # retry, not a second effect.
        idempotency_key=f"discard:{tenant}:{experiment}:{version}",
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


def prepare_get(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    experiment = arguments["experiment_id"]
    return ActionRequest(
        tool=GET.name,
        surface=GET.surface,
        resource=f"{tenant}/experiments/{experiment}",
        payload={},
        idempotency_key=f"get:{tenant}:{experiment}",
    )


def prepare_list(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    query: dict[str, Any] = {"limit": arguments.get("limit", 20)}
    if "status" in arguments:
        query["status"] = arguments["status"]
    return ActionRequest(
        tool=LIST.name,
        surface=LIST.surface,
        # The trailing slash is the collection, and it sits inside the `{tenant}/experiments/`
        # prefix the sandbox already allows - listing does not widen containment.
        resource=f"{tenant}/experiments/",
        payload=query,
        idempotency_key=f"list:{tenant}:{query.get('status', '*')}:{query['limit']}",
    )


def prepare_history(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    experiment = arguments["experiment_id"]
    return ActionRequest(
        tool=HISTORY.name,
        surface=HISTORY.surface,
        resource=f"{tenant}/experiments/{experiment}/history",
        payload={},
        idempotency_key=f"history:{tenant}:{experiment}",
    )
