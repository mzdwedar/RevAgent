"""Identity, trust, policy, approvals: an approval binds to the state of the thing being changed.

`state_snapshot` was `bundle.fingerprint()`, computed once before the proposal loop.
Two consequences the audit named:

(b) within one turn, the second proposal is approved against a world in which the
    first proposal had not yet committed; and
(a) the fingerprint covers the rendered prompt, which contains no timestamps and no
    transcript, so it is near-constant for a given message - an approval stays fresh
    across a world that moved underneath it.

(b) is fixed here: the snapshot is computed per proposal and scoped to the resource
the action touches, so committing one experiment does not invalidate an approval for a
different experiment, and does invalidate one for the same experiment.

(a) needs to know what "the world" is - the experiment's status, its version - which is
a domain question, deferred to SPEC.md. `resource_state` is the
seam it will arrive through.
"""

from __future__ import annotations

from typing import Any

from agentstack.interfaces.wiring import Stack
from agentstack.observability.spans import Tracer
from agentstack.runtime.snapshot import resource_snapshot, world_snapshot
from agentstack.storage.database import Database
from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import Surface

from .conftest import TENANT

BUNDLE = "fp-abc123"
EXP7 = "acme/experiments/exp-7/rollout"
EXP8 = "acme/experiments/exp-8/rollout"


def test_two_resources_in_one_turn_get_different_snapshots() -> None:
    assert resource_snapshot(BUNDLE, EXP7, ()) != resource_snapshot(BUNDLE, EXP8, ())


def test_committing_one_resource_does_not_move_another() -> None:
    """A rollout of exp-7 must not invalidate an approval already given for exp-8."""
    before = resource_snapshot(BUNDLE, EXP8, ())
    after_exp7_committed = resource_snapshot(BUNDLE, EXP8, ())
    assert before == after_exp7_committed


def test_committing_a_resource_does_move_its_own_snapshot() -> None:
    before = resource_snapshot(BUNDLE, EXP7, ())
    after = resource_snapshot(BUNDLE, EXP7, ("receipt-1",))
    assert before != after, (
        "an approval granted before this resource changed must not survive the change"
    )


def test_the_snapshot_still_moves_when_the_prompt_moves() -> None:
    assert resource_snapshot(BUNDLE, EXP7, ()) != resource_snapshot("fp-different", EXP7, ())


# --- the world, read at the act (T43, ADR-0008 rule 3) ---
#
# A surface that can describe its resource says what it is now, and the snapshot is
# that and nothing else. So a commit that runs later, in another process, with no turn
# and no prompt, reads the same snapshot unless the world moved, and an approval against
# a world that has since moved is stale. The Temporal half is `tests/durability/test_commit.py`.

EXPERIMENT = f"{TENANT}/experiments/exp-9"
ROLLOUT_OF = f"{EXPERIMENT}/rollout"


def _observe(stack: Stack, resource: str, surface: Surface = Surface.REGISTRY) -> Any:
    request = ActionRequest(
        tool="t", surface=surface, resource=resource, payload={}, idempotency_key="k"
    )
    tracer = Tracer(run_id="r", session_id="s", versions=stack.deps.versions)
    return stack.deps.gateway.observe(request=request, tracer=tracer)


def _draft(stack: Stack) -> None:
    stack.registry_client.commit(
        EXPERIMENT,
        {"experiment_version": "exp:v1", "hypothesis": "a discount retains", "variant": "20-off"},
    )


def test_the_world_snapshot_carries_nothing_of_the_turn() -> None:
    """What makes it re-readable at the act: no prompt in it, so nothing only the turn knew."""
    world = ("exp:v1", "20-off", "a discount retains")
    assert world_snapshot(ROLLOUT_OF, world) != resource_snapshot(BUNDLE, ROLLOUT_OF, world)
    assert world_snapshot(ROLLOUT_OF, world) != world_snapshot(EXPERIMENT, world)


def test_the_registry_describes_the_experiment_a_rollout_exposes(stack: Stack) -> None:
    assert _observe(stack, ROLLOUT_OF) is None, "nothing drafted, nothing to bind to"
    _draft(stack)
    assert _observe(stack, ROLLOUT_OF) == ("exp:v1", "20-off", "a discount retains")
    assert _observe(stack, EXPERIMENT) == _observe(stack, ROLLOUT_OF)


def test_a_revised_hypothesis_moves_the_world(stack: Stack, app_database: Database) -> None:
    """The approver was shown one hypothesis. A revision is a different experiment to
    them, whatever the version says."""
    _draft(stack)
    before = world_snapshot(ROLLOUT_OF, _observe(stack, ROLLOUT_OF))
    app_database.execute(
        "INSERT INTO draft_revisions"
        " (tenant, experiment_id, experiment_version, revision_no, hypothesis)"
        " VALUES (%s, 'exp-9', 'exp:v1', 2, 'a free month retains')",
        (TENANT,),
    )
    assert world_snapshot(ROLLOUT_OF, _observe(stack, ROLLOUT_OF)) != before


def test_the_rollout_does_not_move_its_own_snapshot(stack: Stack) -> None:
    """Rolling out takes the experiment live. If that moved the snapshot, the retry that
    must deduplicate would read as stale instead, and an unresolved rollout could never
    be settled by running it again."""
    _draft(stack)
    before = _observe(stack, ROLLOUT_OF)
    stack.registry_client.commit(
        ROLLOUT_OF,
        {
            "experiment_version": "exp:v1",
            "percentage": 10,
            "targeting_model_version": "tabpfn-3.5",
            "risk_threshold": 0.61,
            "prior_rollout_event": 0,
        },
    )
    assert stack.registry_client.rollouts, "the rollout happened"
    assert _observe(stack, ROLLOUT_OF) == before


def test_a_surface_that_cannot_describe_its_resource_falls_back(stack: Stack) -> None:
    """The reference API has no resource model, so `act` binds as it did before T43."""
    assert _observe(stack, EXP7, Surface.API) is None


def test_outside_containment_is_described_as_nothing(stack: Stack) -> None:
    """Observed before the gateway has decided anything, so it refuses nothing itself: the
    containment refusal happens in `execute`, where it is audited."""
    assert _observe(stack, "other/experiments/exp-9/rollout") is None
