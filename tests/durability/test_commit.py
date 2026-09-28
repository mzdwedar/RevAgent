"""T43: the commit activity, where the irreversible act happens, bound to the world at the act.

The run drafts, proposes the rollout, parks, asks, and is answered (T41, T42). Then it
acts: the `commit` activity reads the proposal from the rollout turn's checkpoint, reads
the world as the registry describes it *now*, mints the envelope, and calls the gateway.
Nothing but ids reaches it from the workflow (ADR-0008 rule 3).

Every refusal here is counted in the audit trail, because every gateway attempt leaves
a record there: one record is one attempt.
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from agentstack.execution.surfaces import SurfaceTimeout
from agentstack.interfaces.wiring import Stack, answer
from agentstack.observability.audit import AuditRecord
from agentstack.policy.approvers import ApprovalReply
from agentstack.runtime.temporal.activities import NOTHING_APPROVED, WORLD_UNREADABLE
from agentstack.runtime.temporal.client import notify_answer
from agentstack.runtime.temporal.contracts import CommitIntent, CommitOutcome, RunProgress
from agentstack.runtime.waits import RECONCILE, ResumeEvent, WaitStore, resume
from agentstack.storage.database import Database
from agentstack.tools.spec import Surface
from tests.durability.test_approval_wait import propose_and_wait
from tests.durability.test_approval_wait import stack as stack  # the same fixture
from tests.durability.test_slack_answer import APPROVER, approvals, slack_click
from tests.fitness.test_trigger_to_candidate import RULE, StubScorer
from tests.temporal_support import DraftingEngine, activities_for, progress_until, turns_for

pytestmark = pytest.mark.usefixtures("fixture_dataset")


@pytest.fixture(autouse=True)
def _approver(stack: Stack) -> None:
    stack.approver_directory.add(
        tenant="acme", slack_user_id=APPROVER, principal="ana@acme", added_by="t43"
    )


def reply(parked: RunProgress, *, approved: bool = True) -> ApprovalReply:
    cycle = parked.cycles[0]
    return ApprovalReply(
        run_id=parked.run_id,
        wait_id=parked.awaiting_approval or "",
        experiment_version=cycle.experiment_version or "",
        data_as_of=cycle.data_as_of,
        approved=approved,
        slack_user_id=APPROVER,
        channel="C1",
    )


def rollout_audit(stack: Stack, run_id: str) -> list[AuditRecord]:
    """What the gateway recorded at the act: one record per attempt.

    The first rollout record is the turn's, when the gateway stopped the proposal at the
    approval boundary and the run parked. Everything after it is the commit activity's.
    """
    proposed, *acts = [r for r in stack.audit.for_run(run_id) if r.resource.endswith("/rollout")]
    assert (proposed.policy_decision, proposed.outcome) == ("approval.required", "refused")
    return acts


async def acted(handle: Any, count: int = 1) -> RunProgress:
    return await progress_until(handle, lambda p: len(p.commits) >= count)


def test_an_approved_rollout_commits_once_at_the_act(stack: Stack, app_database: Database) -> None:
    async def approve(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await answer(stack, env.client, **slack_click(parked))
        return await acted(handle)

    run_id, parked, done = propose_and_wait(stack, app_database, then=approve)

    assert done.commits == (CommitOutcome(status="committed"),)
    ((resource, payload),) = stack.registry_client.rollouts
    assert resource == "acme/experiments/exp-7/rollout"
    assert payload["experiment_version"] == parked.cycles[0].experiment_version
    assert payload["percentage"] == 10
    (record,) = rollout_audit(stack, run_id)
    assert record.outcome == "committed" and record.approval_id is not None
    assert record.policy_decision == "allow", "a person's approval, not a rule's"

    # Criterion 36: the act again, as a retry after its completion was lost would run it.
    # Same ids, same content key: the ledger answers, and the registry isn't asked again.
    assert commit_again(stack, app_database, parked) == CommitOutcome(status="deduplicated")
    assert len(stack.registry_client.rollouts) == 1


def commit_again(stack: Stack, db: Database, parked: RunProgress, *, wait_id: str = "") -> Any:
    """The commit activity run directly, outside any workflow: an attempt, as Temporal
    would make one, with the ids a `CommitIntent` carries."""
    cycle = parked.cycles[0]
    activities = activities_for(
        db, scorer=StubScorer(), rule=RULE, turns=turns_for(stack, DraftingEngine())
    )
    return ActivityEnvironment().run(
        activities.commit,
        CommitIntent(
            run_id=parked.run_id,
            wait_id=wait_id or parked.awaiting_approval or "",
            experiment_id=cycle.experiment_id,
            data_as_of=cycle.data_as_of,
            kind=cycle.kind,
        ),
    )


def test_an_action_the_turn_never_proposed_is_not_committed(
    stack: Stack, app_database: Database
) -> None:
    """What commits is what the person was shown, or nothing. A wait naming any other
    action (one no checkpointed proposal of the rollout turn prepares to) commits nothing,
    even with a person's approval for it on record."""
    _, parked, _ = propose_and_wait(stack, app_database)
    other = stack.waits.park(
        run_id=parked.run_id,
        kind="human_approval",
        state_snapshot="s",
        action_fingerprint="an-action-nobody-proposed",
        approval_summary="something else",
    )
    stack.approvals.grant_for_fingerprint(
        run_id=parked.run_id,
        action_fingerprint="an-action-nobody-proposed",
        state_snapshot="s",
        approver="ana@acme",
        summary="something else",
    )

    with pytest.raises(ApplicationError) as refused:
        commit_again(stack, app_database, parked, wait_id=other.wait_id)

    assert refused.value.type == NOTHING_APPROVED and refused.value.non_retryable
    assert stack.registry_client.rollouts == []


class CannotDescribe:
    """A registry that can no longer say what the experiment is."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def read(self, resource: str, query: dict[str, Any]) -> Any:
        return self.inner.read(resource, query)

    def state(self, resource: str) -> None:
        del resource

    def commit(self, resource: str, payload: dict[str, Any]) -> Any:
        return self.inner.commit(resource, payload)


def test_a_world_that_cannot_be_read_is_not_acted_on(stack: Stack, app_database: Database) -> None:
    """With nothing to compare the approval against, staleness can't be checked, so the
    act doesn't happen: refused once, not retried, not guessed."""

    _, parked, _ = propose_and_wait(stack, app_database)
    stack.coordinator.apply(reply(parked))
    stack.deps.gateway.surfaces[Surface.REGISTRY] = CannotDescribe(
        stack.deps.gateway.surfaces[Surface.REGISTRY]
    )

    with pytest.raises(ApplicationError) as refused:
        commit_again(stack, app_database, parked)

    assert refused.value.type == WORLD_UNREADABLE and refused.value.non_retryable
    assert stack.registry_client.rollouts == []


def test_a_world_that_moved_after_the_approval_refuses_at_the_act(
    stack: Stack, app_database: Database
) -> None:
    """Criterion 33. The person approved the draft they were shown; before the run could
    act, its hypothesis was revised. The approval is for a world that no longer exists."""

    async def approve_then_revise(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        stack.coordinator.apply(reply(parked))
        cycle = parked.cycles[0]
        app_database.execute(
            "INSERT INTO draft_revisions"
            " (tenant, experiment_id, experiment_version, revision_no, hypothesis)"
            " VALUES ('acme', %s, %s, 2, 'a different offer than the one approved')",
            (cycle.experiment_id, cycle.experiment_version),
        )
        await notify_answer(
            env.client, run_id=parked.run_id, wait_id=parked.awaiting_approval or ""
        )
        return await acted(handle)

    run_id, _, done = propose_and_wait(stack, app_database, then=approve_then_revise)

    assert done.commits == (CommitOutcome(status="refused", refusal="ApprovalStale"),)
    assert stack.registry_client.rollouts == []
    (record,) = rollout_audit(stack, run_id)  # one attempt, not a retry loop
    assert (record.policy_decision, record.outcome) == ("approval.stale", "refused")


def test_a_wake_up_nobody_authorised_meets_the_gateway(
    stack: Stack, app_database: Database
) -> None:
    """Criterion 39, second half. The signal grants nothing: a run woken with no answer
    recorded (a forged or mistaken signal) acts, and the gateway finds no approval."""

    async def forged(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await notify_answer(
            env.client, run_id=parked.run_id, wait_id=parked.awaiting_approval or ""
        )
        return await acted(handle)

    run_id, parked, done = propose_and_wait(stack, app_database, then=forged)

    assert done.commits == (CommitOutcome(status="refused", refusal="ApprovalRequired"),)
    assert stack.registry_client.rollouts == []
    assert approvals(app_database) == 0
    (record,) = rollout_audit(stack, run_id)
    assert (record.policy_decision, record.outcome) == ("approval.required", "refused")
    wait = WaitStore(db=app_database).get(parked.awaiting_approval or "")
    assert wait is not None and not wait.satisfied, "the question is still unanswered"


def test_a_no_is_refused_at_the_act(stack: Stack, app_database: Database) -> None:
    async def refuse(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await answer(stack, env.client, **slack_click(parked, approve=False))
        return await acted(handle)

    run_id, _, done = propose_and_wait(stack, app_database, then=refuse)

    assert done.commits == (CommitOutcome(status="refused", refusal="ApprovalRequired"),)
    assert stack.registry_client.rollouts == []
    assert len(rollout_audit(stack, run_id)) == 1


class LosesTheFirstRolloutAnswer:
    """The registry applies the rollout and the answer is lost, once: the failure the
    two-phase ledger exists for. Everything else passes straight through."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.rollouts = 0

    def read(self, resource: str, query: dict[str, Any]) -> Any:
        return self.inner.read(resource, query)

    def state(self, resource: str) -> Any:
        return self.inner.state(resource)

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        receipt = str(self.inner.commit(resource, payload))
        if resource.endswith("/rollout"):
            self.rollouts += 1
            if self.rollouts == 1:
                raise SurfaceTimeout(f"{resource} applied, answer lost")
        return receipt


def test_an_unresolved_rollout_parks_until_reconciled_then_deduplicates(
    stack: Stack, app_database: Database
) -> None:
    """Criterion 34: one attempt, then the run parks. Retried blind, the second attempt
    would meet the claim and refuse at best, or roll out twice at worst."""
    surface = LosesTheFirstRolloutAnswer(stack.deps.gateway.surfaces[Surface.REGISTRY])
    stack.deps.gateway.surfaces[Surface.REGISTRY] = surface
    seen: dict[str, Any] = {}

    async def approve_and_reconcile(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await answer(stack, env.client, **slack_click(parked))
        unresolved = await progress_until(handle, lambda p: p.reconciling is not None)
        seen["unresolved"] = unresolved
        seen["keys"] = stack.ledger.unresolved_keys()

        # What a reconciler does: ask the surface what happened, settle the claim with
        # what it says, and satisfy the wait. Then wake the run.
        (key,) = seen["keys"]
        stack.ledger.finalize(key, "rollout-reconciled")
        wait = WaitStore(db=app_database).get(unresolved.reconciling or "")
        assert wait is not None
        resume(
            stack.waits,
            ResumeEvent(
                run_id=wait.run_id,
                wait_id=wait.wait_id,
                state_snapshot=wait.state_snapshot,
                payload={"reconciled_by": "ops", "receipt": "rollout-reconciled"},
            ),
        )
        await notify_answer(env.client, run_id=parked.run_id, wait_id=wait.wait_id)
        return await acted(handle, 2)

    run_id, _, done = propose_and_wait(stack, app_database, then=approve_and_reconcile)

    unresolved: RunProgress = seen["unresolved"]
    (first,) = unresolved.commits
    assert first.status == "unresolved" and first.wait_id == unresolved.reconciling
    parked = WaitStore(db=app_database).get(first.wait_id or "")
    assert parked is not None and parked.kind == RECONCILE and parked.action_fingerprint
    assert len(seen["keys"]) == 1

    assert done.commits[1] == CommitOutcome(status="deduplicated")
    assert done.reconciling is None
    assert surface.rollouts == 1, "the surface was asked to roll out once"
    assert len(stack.registry_client.rollouts) == 1
    outcomes = [r.outcome for r in rollout_audit(stack, run_id)]
    assert outcomes == ["unresolved", "deduplicated"]
    assert stack.ledger.unresolved_keys() == ()
