"""The registry tools are narrow, and each stage is shown only its own (SPEC-registry.md).

A tool's blast radius is bounded twice: by its shape, and by which runs are shown it at
all. This file holds the second bound as an exact matrix - not "rollout is absent from
drafting" but "drafting is shown precisely these tools" - because an assertion of
absence passes just as happily when a new, wider tool is added beside it.

The first bound - the shape - is the narrowness bar below (T30): no generic setters, no
batch arguments, only the rollout names a percentage, every write names its version,
and one scope per blast radius.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from agentstack.execution.gateway import UnresolvedEffect
from agentstack.execution.surfaces import (
    PostgresRegistryClient,
    RegistryClient,
    SurfaceClient,
    SurfaceRefused,
    SurfaceTimeout,
)
from agentstack.interfaces.wiring import Stack, envelope_for
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import new_run
from agentstack.storage.database import Database
from agentstack.tools.action import ActionRequest
from agentstack.tools.catalog import build_registry
from agentstack.tools.experiments import (
    DRAFT_STAGE,
    EVALUATION_STAGE,
    LIST,
    ROLLOUT_STAGE,
    STATUSES,
    prepare_abstain,
    prepare_discard,
    prepare_halt,
    prepare_list,
    prepare_revise,
)
from agentstack.tools.spec import Approval, Surface, ToolSpec

from .conftest import TENANT, USER

READS = {"get_experiment", "list_experiments"}

EXPOSURE = {
    DRAFT_STAGE: READS
    | {"create_experiment_draft", "revise_draft_hypothesis", "discard_experiment_draft"},
    EVALUATION_STAGE: READS | {"get_rollout_history", "record_abstention", "halt_rollout"},
    ROLLOUT_STAGE: READS | {"get_rollout_history", "roll_out_variant_to_percentage"},
}


@pytest.mark.parametrize("stage", sorted(EXPOSURE))
def test_each_experiment_stage_is_shown_exactly_its_own_tools(stage: str) -> None:
    names = {spec.name for spec in build_registry().expose_for(tenant=TENANT, stage=stage)}

    assert names == EXPOSURE[stage]


def test_a_drafting_turn_cannot_see_the_rollout_tool() -> None:
    """The gap this task closed: drafting and rolling out shared one stage, so a turn
    writing a reversible draft was also offered the one irreversible act."""
    names = {s.name for s in build_registry().expose_for(tenant=TENANT, stage=DRAFT_STAGE)}

    assert "roll_out_variant_to_percentage" not in names


def test_the_experiment_stages_are_distinct() -> None:
    assert len({DRAFT_STAGE, EVALUATION_STAGE, ROLLOUT_STAGE}) == 3


def test_the_menus_are_the_sizes_the_spec_names() -> None:
    """Spec criterion 2, as numbers: a sixth drafting tool is a decision, not a drift."""
    assert {stage: len(tools) for stage, tools in EXPOSURE.items()} == {
        DRAFT_STAGE: 5,
        EVALUATION_STAGE: 5,
        ROLLOUT_STAGE: 4,
    }


# --- the narrowness bar (SPEC-registry.md § What "narrow" means here) ---
#
# Held over every registry tool the catalog has, not over a list typed here, so a tenth
# tool is held to it the moment it is registered.

REGISTRY_TOOLS = [s for s in build_registry().specs() if s.surface is Surface.REGISTRY]
WRITE_TOOLS = [s for s in REGISTRY_TOOLS if s.side_effecting]


def properties(spec: ToolSpec) -> dict[str, Any]:
    return dict(spec.input_schema["properties"])


def test_all_nine_registry_tools_are_registered() -> None:
    """Spec criterion 1. `test_tool_registry.py` holds each one's metadata."""
    assert {s.name for s in REGISTRY_TOOLS} == set().union(*EXPOSURE.values())
    assert len(REGISTRY_TOOLS) == 9


@pytest.mark.parametrize("spec", REGISTRY_TOOLS, ids=lambda s: s.name)
def test_no_registry_tool_is_a_generic_setter(spec: ToolSpec) -> None:
    """A transition is a tool; its target state is its name. A tool taking `status`,
    `fields`, `patch` or `updates` could be told to do anything. The one `status` is the
    list read's filter, which names what to show, not what to become."""
    setters = {"fields", "patch", "updates"} | ({"status"} if spec is not LIST else set())

    assert not setters & set(properties(spec))


@pytest.mark.parametrize("spec", REGISTRY_TOOLS, ids=lambda s: s.name)
def test_a_registry_tool_names_one_thing_at_a_time(spec: ToolSpec) -> None:
    """No batch, no list of ids: every argument is a single value, so one call touches
    one experiment."""
    kinds = {p["type"] for p in properties(spec).values()}

    assert not kinds & {"array", "object"}


def test_only_the_rollout_can_name_a_percentage() -> None:
    """A halt with a `percentage` argument is a rollout with a nicer name."""
    assert [s.name for s in REGISTRY_TOOLS if "percentage" in properties(s)] == [
        "roll_out_variant_to_percentage"
    ]


@pytest.mark.parametrize("spec", WRITE_TOOLS, ids=lambda s: s.name)
def test_every_write_names_the_version_it_was_prepared_against(spec: ToolSpec) -> None:
    """A call prepared against one frozen cohort cannot land on another."""
    assert "experiment_version" in spec.input_schema["required"]


def test_only_the_moves_that_start_from_a_state_name_one() -> None:
    """Audit C2: a rollout, a revision and an abstention are keyed on where they start,
    so each names it - and nothing else does. Each is one integer the model read back
    from a registry read: bounded below, not a list, not an id string that could carry
    a second experiment. A halt and a discard end a version once, and name no prior."""
    priors = {
        s.name: sorted(name for name in properties(s) if name.startswith("prior_"))
        for s in WRITE_TOOLS
    }

    assert {name: names for name, names in priors.items() if names} == {
        "roll_out_variant_to_percentage": ["prior_rollout_event"],
        "revise_draft_hypothesis": ["prior_revision"],
        "record_abstention": ["prior_event"],
    }
    for spec in WRITE_TOOLS:
        for name in priors[spec.name]:
            declared = properties(spec)[name]
            assert declared["type"] == "integer"
            assert declared["minimum"] >= 0
            assert name in spec.input_schema["required"]


def test_each_scope_is_one_blast_radius() -> None:
    """Five scopes, and a grant of one never implies another: drafting cannot halt,
    annotating cannot stop anything, and only the rollout scope reaches customers."""
    by_scope: dict[str, set[str]] = {}
    for spec in REGISTRY_TOOLS:
        by_scope.setdefault(spec.scope, set()).add(spec.name)

    assert by_scope == {
        "experiments:read": {"get_experiment", "list_experiments", "get_rollout_history"},
        "experiments:draft": {
            "create_experiment_draft",
            "revise_draft_hypothesis",
            "discard_experiment_draft",
        },
        "experiments:annotate": {"record_abstention"},
        "experiments:halt": {"halt_rollout"},
        "experiments:rollout": {"roll_out_variant_to_percentage"},
    }


def test_the_stack_writes_the_real_registry(stack: Stack) -> None:
    """The fake is for tests that ask for it. A stack wired from `build_stack` records
    drafts and rollouts where they outlive the process - T25's whole point."""
    assert isinstance(stack.registry_client, PostgresRegistryClient)


def test_a_read_is_a_read() -> None:
    """No effect, no approval, one scope: a read that needed a tier would be a write
    wearing a read's name, and one that took a write scope would widen every grant."""
    reads = [s for s in build_registry().specs() if s.name in READS | {"get_rollout_history"}]

    assert len(reads) == 3
    for spec in reads:
        assert spec.side_effecting is False
        assert spec.approval is Approval.NONE
        assert spec.scope == "experiments:read"
        assert spec.surface is Surface.REGISTRY


def test_a_list_is_bounded_in_its_schema() -> None:
    limit = LIST.input_schema["properties"]["limit"]

    assert (limit["minimum"], limit["maximum"]) == (1, 50)
    assert LIST.input_schema["properties"]["status"]["enum"] == STATUSES


def test_the_collection_read_stays_inside_the_sandbox_prefix() -> None:
    """Listing names the collection with a trailing slash, which the existing
    `{tenant}/experiments/` prefix already covers - containment is not widened."""
    request = prepare_list({"tenant": TENANT})

    assert request.resource == f"{TENANT}/experiments/"
    assert request.payload == {"limit": 20}


@pytest.mark.parametrize(
    ("tool", "arguments", "resource"),
    [
        ("get_experiment", {"experiment_id": "exp-7"}, f"{TENANT}/experiments/exp-7"),
        ("get_rollout_history", {"experiment_id": "exp-7"}, f"{TENANT}/experiments/exp-7/history"),
        ("list_experiments", {"status": "live", "limit": 5}, f"{TENANT}/experiments/"),
    ],
)
def test_each_read_asks_the_surface_through_a_resource_it_serves(
    stack: Stack, tool: str, arguments: dict[str, object], resource: str
) -> None:
    """Prepared through the registry, validated, and read back through the real store:
    a read tool whose resource the surface refused would be a menu item that never works."""
    registry = build_registry()
    exposed = registry.expose_for(tenant=TENANT, stage=ROLLOUT_STAGE)

    request = registry.prepare(tool, {"tenant": TENANT, **arguments}, exposed=exposed)

    assert request.resource == resource
    assert isinstance(stack.registry_client.read(request.resource, request.payload), dict)


# --- spec criterion 5: an answer lost after the effect is still one effect ---

REGISTRY_SCOPES = frozenset(
    {
        "experiments:read",
        "experiments:draft",
        "experiments:annotate",
        "experiments:halt",
        "experiments:rollout",
    }
)
EXPERIMENT_AT = f"{TENANT}/experiments/exp-7"
VERSION = "exp:abc123"
SEED_DRAFT = {
    "experiment_version": VERSION,
    "hypothesis": "a discount retains at-risk customers",
    "variant": "20-percent-off",
}
SEED_ROLLOUT = {
    "experiment_version": VERSION,
    "percentage": 10,
    "targeting_model_version": "tabpfn-3.5",
    "risk_threshold": 0.61,
    "prior_rollout_event": 0,
}


@dataclass(frozen=True)
class AnswerLost:
    """The real store, with the answer dropped after every write has applied."""

    inner: SurfaceClient

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        return self.inner.read(resource, query)

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        self.inner.commit(resource, payload)
        raise SurfaceTimeout(f"{resource}: applied, answer lost")


Prepare = Callable[[Mapping[str, Any]], ActionRequest]

# Every registry write that can be retried, its own arguments, and the state it acts on.
WRITES: dict[str, tuple[Prepare, dict[str, Any], str]] = {
    "revise_draft_hypothesis": (
        prepare_revise,
        {"hypothesis": "a smaller discount retains them too"},
        "draft",
    ),
    "discard_experiment_draft": (prepare_discard, {"reason": "the cohort is too small"}, "draft"),
    "record_abstention": (
        prepare_abstain,
        {"explanation": "the guardrail metric is inside its noise band"},
        "live",
    ),
    "halt_rollout": (prepare_halt, {"reason": "churn rose in the variant"}, "live"),
}


def _history_rows(db: Database) -> int:
    row = db.fetch_one(
        "SELECT (SELECT count(*) FROM draft_revisions) + (SELECT count(*) FROM registry_events)"
    )
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("tool", sorted(WRITES))
def test_a_lost_answer_and_a_retry_leave_one_row(
    stack: Stack, app_database: Database, tool: str
) -> None:
    """Applied, answer lost, retried: the retry is refused as unresolved and the store
    holds one row, not two - against the real store, not the fake."""
    prepare, arguments, state = WRITES[tool]
    stack.registry_client.commit(EXPERIMENT_AT, SEED_DRAFT)
    if state == "live":
        stack.registry_client.commit(f"{EXPERIMENT_AT}/rollout", SEED_ROLLOUT)
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    run = stack.runs.ensure(
        new_run(session_id=session.session_id, tenant=TENANT, user=USER, channel="test")
    )
    view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
    # The prior state a write names, read the way the model reads it (audit C2).
    read = {
        "prior_revision": stack.registry_client.read(EXPERIMENT_AT, {})["revision_no"],
        "prior_event": stack.registry_client.read(f"{EXPERIMENT_AT}/history", {})["latest_event"],
    }
    named = build_registry().spec(tool).input_schema["properties"]
    request = prepare(
        {
            "tenant": TENANT,
            "experiment_id": "exp-7",
            "experiment_version": VERSION,
            **arguments,
            **{key: value for key, value in read.items() if key in named},
        }
    )
    before = _history_rows(app_database)

    def attempt() -> None:
        stack.deps.gateway.execute(
            request=request,
            spec=build_registry().spec(tool),
            envelope=envelope_for(view, scopes=REGISTRY_SCOPES),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=Tracer(
                run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions
            ),
        )

    stack.deps.gateway.surfaces[Surface.REGISTRY] = AnswerLost(stack.registry_client)
    with pytest.raises(UnresolvedEffect):
        attempt()
    stack.deps.gateway.surfaces[Surface.REGISTRY] = stack.registry_client
    with pytest.raises(UnresolvedEffect, match="(?i)reconcile"):
        attempt()

    assert _history_rows(app_database) == before + 1


# --- audit C2: a move is keyed on where it started, through the whole path ---
#
# The ledger and the store both used to hold "the same target" as "the same effect", so
# an approved write back to an earlier state came back `deduplicated=True` with the
# first write's receipt, and nothing moved. These go through `gateway.execute` - the
# idempotency ledger and the surface together - over both clients.


@pytest.fixture(params=["fake", "postgres"])
def registry(request: pytest.FixtureRequest, stack: Stack) -> SurfaceClient:
    """The client the gateway commits to. The ledger is Postgres either way."""
    client: SurfaceClient = RegistryClient() if request.param == "fake" else stack.registry_client
    stack.deps.gateway.surfaces[Surface.REGISTRY] = client
    return client


@dataclass(frozen=True)
class Caller:
    """Registry tools called the way `act` calls them: prepared, validated, executed."""

    stack: Stack
    client: SurfaceClient

    def call(self, tool: str, **arguments: Any) -> tuple[str, bool]:
        stack = self.stack
        session = stack.resolver.start(user_id=USER, tenant=TENANT)
        run = stack.runs.ensure(
            new_run(session_id=session.session_id, tenant=TENANT, user=USER, channel="test")
        )
        view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
        registry = build_registry()
        spec = registry.spec(tool)
        request = registry.prepare(
            tool,
            {
                "tenant": TENANT,
                "experiment_id": "exp-7",
                "experiment_version": VERSION,
                **arguments,
            },
            exposed=(spec,),
        )
        if spec.approval is Approval.ALWAYS:
            # A person said yes to exactly this request, which now names where it starts.
            stack.approvals.grant(
                run_id=run.run_id,
                request=request,
                state_snapshot="s",
                approver="growth-oncall",
                summary=f"{tool} {request.payload}",
            )
        result = stack.deps.gateway.execute(
            request=request,
            spec=spec,
            envelope=envelope_for(view, scopes=REGISTRY_SCOPES),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=Tracer(
                run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions
            ),
        )
        return result.receipt, result.deduplicated

    def history(self) -> dict[str, Any]:
        return self.client.read(f"{EXPERIMENT_AT}/history", {})

    def roll_out(self, percentage: int, prior: int | None = None) -> tuple[str, bool]:
        return self.call(
            "roll_out_variant_to_percentage",
            percentage=percentage,
            targeting_model_version="tabpfn-3.5",
            risk_threshold=0.61,
            prior_rollout_event=self.history()["latest_rollout_event"] if prior is None else prior,
        )


def test_an_approved_ramp_down_is_not_a_duplicate(stack: Stack, registry: SurfaceClient) -> None:
    """The finding, reproduced and closed: 10%, 25%, back to 10%, each with a human's
    grant. The third came back deduplicated with the first's receipt, still at 25%."""
    registry.commit(EXPERIMENT_AT, SEED_DRAFT)
    caller = Caller(stack, registry)

    results = [caller.roll_out(pct) for pct in (10, 25, 10)]

    assert [dedup for _, dedup in results] == [False, False, False]
    assert len({receipt for receipt, _ in results}) == 3
    history = caller.history()
    assert [e["percentage"] for e in history["events"]] == [10, 25, 10]
    assert history["current_exposure"] == 10


def test_two_rollouts_approved_against_one_state_land_once(
    stack: Stack, registry: SurfaceClient
) -> None:
    """Both approved, both from the state after 10%. The first moves exposure; the second
    is refused with nothing applied - not answered with somebody else's receipt."""
    registry.commit(EXPERIMENT_AT, SEED_DRAFT)
    caller = Caller(stack, registry)
    caller.roll_out(10)
    prior = caller.history()["latest_rollout_event"]

    caller.roll_out(25, prior=prior)
    with pytest.raises(SurfaceRefused):
        caller.roll_out(50, prior=prior)

    history = caller.history()
    assert [e["percentage"] for e in history["events"]] == [10, 25]
    assert history["current_exposure"] == 25


def test_a_draft_revised_back_to_its_first_wording_says_it(
    stack: Stack, registry: SurfaceClient
) -> None:
    registry.commit(EXPERIMENT_AT, SEED_DRAFT)
    caller = Caller(stack, registry)
    first, second = SEED_DRAFT["hypothesis"], "a smaller discount retains them too"

    results = [
        caller.call(
            "revise_draft_hypothesis",
            hypothesis=wording,
            prior_revision=registry.read(EXPERIMENT_AT, {})["revision_no"],
        )
        for wording in (second, first)
    ]

    assert [dedup for _, dedup in results] == [False, False]
    after = registry.read(EXPERIMENT_AT, {})
    assert (after["hypothesis"], after["revision_no"]) == (first, 3)


def test_two_cycles_abstaining_for_the_same_reason_are_two_records(
    stack: Stack, registry: SurfaceClient
) -> None:
    """ "Insufficient sample" is what every early cycle says. The second cycle to say it
    was deduplicated into the first, and left no record."""
    registry.commit(EXPERIMENT_AT, SEED_DRAFT)
    caller = Caller(stack, registry)
    caller.roll_out(10)

    results = [
        caller.call(
            "record_abstention",
            explanation="insufficient sample",
            prior_event=caller.history()["latest_event"],
        )
        for _ in range(2)
    ]

    assert [dedup for _, dedup in results] == [False, False]
    assert len({receipt for receipt, _ in results}) == 2
