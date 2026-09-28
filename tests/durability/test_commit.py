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
from langgraph.checkpoint.memory import InMemorySaver
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.execution.surfaces import SurfaceTimeout
from agentstack.interfaces.wiring import Stack, answer, build_stack
from agentstack.observability.audit import AuditRecord
from agentstack.observability.spans import CollectingSink
from agentstack.policy.approvers import ApprovalReply
from agentstack.runtime.drafting import intended_rollout
from agentstack.runtime.temporal.activities import NOTHING_APPROVED, WORLD_UNREADABLE
from agentstack.runtime.temporal.client import notify_answer
from agentstack.runtime.temporal.contracts import (
    NOT_ANSWERED,
    CommitIntent,
    CommitOutcome,
    RunProgress,
)
from agentstack.runtime.waits import RECONCILE, ResumeEvent, WaitStore, resume
from agentstack.storage.database import Database
from agentstack.tools.experiments import ROLLOUT
from agentstack.tools.spec import Surface
from tests.durability.test_approval_wait import propose_and_wait
from tests.durability.test_approval_wait import stack as stack  # the same fixture
from tests.durability.test_slack_answer import APPROVER, slack_click
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


def commit_again(
    stack: Stack,
    db: Database,
    parked: RunProgress,
    *,
    wait_id: str = "",
    traces: CollectingSink | None = None,
) -> Any:
    """The commit activity run directly, outside any workflow: an attempt, as Temporal
    would make one, with the ids a `CommitIntent` carries."""
    cycle = parked.cycles[0]
    activities = activities_for(
        db,
        scorer=StubScorer(),
        rule=RULE,
        turns=turns_for(stack, DraftingEngine()),
        traces=traces,
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


def held(parked: RunProgress, stack: Stack, **changes: Any) -> dict[str, Any]:
    """The arguments the frozen cohort's rollout has, with `changes` made to them."""
    cohort = FrozenCohortStore(db=stack.runs.db).get(
        tenant="acme",
        experiment_id=parked.cycles[0].experiment_id,
        experiment_version=parked.cycles[0].experiment_version or "",
    )
    assert cohort is not None
    return intended_rollout(cohort) | changes


@pytest.mark.parametrize(
    "holds",
    ["nothing", "another-action", "arguments-no-longer-valid"],
)
def test_an_action_the_wait_does_not_hold_whole_is_not_committed(
    stack: Stack, app_database: Database, holds: str
) -> None:
    """What commits is what the person was shown, or nothing (A1, H4). The action is read
    from the wait, validated against the tool's schema again and held to the fingerprint
    beside it. A wait that holds no action, holds one its fingerprint does not name, or
    holds arguments the schema now refuses commits nothing, even answered and with a
    person's approval for its fingerprint on record. The refusal is audited here, because
    the gateway never sees it, and traced."""
    _, parked, _ = propose_and_wait(stack, app_database)
    arguments = held(parked, stack)
    action: dict[str, Any] = {
        "nothing": {},
        "another-action": {"action_tool": ROLLOUT.name, "action_arguments": arguments},
        "arguments-no-longer-valid": {
            "action_tool": ROLLOUT.name,
            "action_arguments": arguments | {"percentage": "lots"},
        },
    }[holds]
    other = stack.waits.park(
        run_id=parked.run_id,
        kind="human_approval",
        state_snapshot="s",
        action_fingerprint="an-action-nobody-proposed",
        approval_summary="something else",
        **action,
    )
    resume(stack.waits, ResumeEvent(parked.run_id, other.wait_id, "s", {"approved_by": "ana"}))
    stack.approvals.grant_for_fingerprint(
        run_id=parked.run_id,
        action_fingerprint="an-action-nobody-proposed",
        state_snapshot="s",
        approver="ana@acme",
        summary="something else",
    )
    sink = CollectingSink()

    with pytest.raises(ApplicationError) as refused:
        commit_again(stack, app_database, parked, wait_id=other.wait_id, traces=sink)

    assert refused.value.type == NOTHING_APPROVED and refused.value.non_retryable
    assert stack.registry_client.rollouts == []
    (record,) = [r for r in stack.audit.for_run(parked.run_id) if r.wait_id == other.wait_id]
    assert (record.policy_decision, record.outcome) == (f"refused:{NOTHING_APPROVED}", "refused")
    assert record.state_snapshot == "s"
    (span,) = [s for s in sink.spans if s.name == "commit.refuse"]
    assert span.attributes["refusal"] == NOTHING_APPROVED


def test_the_approved_action_is_read_from_the_wait_not_the_checkpoint(
    stack: Stack, app_database: Database
) -> None:
    """A1, H4. The act used to rebuild the proposal from the rollout turn's checkpoint, so
    the first checkpoint change it could not read turned every parked "yes" into
    `NothingApproved`. The wait holds the action now. Here the act runs in a process
    whose checkpointer has never seen this run: no checkpoint at all, the limit of every
    incompatible one. The person's answer still commits, exactly what they were shown."""
    _, parked, _ = propose_and_wait(stack, app_database)
    stack.coordinator.apply(reply(parked))
    elsewhere = build_stack(app_database, InMemorySaver())

    assert commit_again(elsewhere, app_database, parked) == CommitOutcome(status="committed")

    ((_, payload),) = stack.registry_client.rollouts
    wait = stack.waits.get(parked.awaiting_approval or "")
    assert wait is not None and wait.action_arguments is not None
    assert payload["percentage"] == wait.action_arguments["percentage"]
    assert payload["experiment_version"] == wait.action_arguments["experiment_version"]


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
    sink = CollectingSink()

    with pytest.raises(ApplicationError) as refused:
        commit_again(stack, app_database, parked, traces=sink)

    assert refused.value.type == WORLD_UNREADABLE and refused.value.non_retryable
    assert stack.registry_client.rollouts == []
    # A1, H3: refused before the gateway, so audited here, with the wait it was about.
    (record,) = [r for r in rollout_audit(stack, parked.run_id) if r.outcome == "refused"]
    assert record.policy_decision == f"refused:{WORLD_UNREADABLE}"
    assert record.wait_id == parked.awaiting_approval
    assert [s.attributes["refusal"] for s in sink.spans if s.name == "commit.refuse"] == [
        WORLD_UNREADABLE
    ]


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


def test_a_wake_up_nobody_answered_moves_nothing_and_a_later_yes_still_commits(
    stack: Stack, app_database: Database
) -> None:
    """Criterion 39, second half, and the floor rule "no run advancing past an unsatisfied
    wait" (A1, H2).

    The signal grants nothing, and now it moves nothing either. A run woken with no
    answer recorded (a stray or forged signal) reads the wait at the act, finds it
    unanswered, records the wake and goes back to the question it was on: no gateway
    attempt, no second post of the question. The answer that does come later wakes it
    again, and commits. (This test used to assert the wait was still unanswered *after*
    the run had moved past it, which was the bug.)"""
    seen: dict[str, Any] = {}

    async def stray_then_answered(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await notify_answer(
            env.client, run_id=parked.run_id, wait_id=parked.awaiting_approval or ""
        )
        seen["woken"] = await progress_until(
            handle, lambda p: len(p.commits) == 1 and p.awaiting_approval is not None
        )
        seen["wait"] = WaitStore(db=app_database).get(parked.awaiting_approval or "")
        await answer(stack, env.client, **slack_click(parked))
        return await acted(handle, 2)

    run_id, parked, done = propose_and_wait(stack, app_database, then=stray_then_answered)

    woken: RunProgress = seen["woken"]
    assert woken.commits == (CommitOutcome(status=NOT_ANSWERED, wait_id=parked.awaiting_approval),)
    assert woken.awaiting_approval == parked.awaiting_approval, "back on the same question"
    assert woken.answered == (), "the stray wake is spent, not kept as an answer"
    assert seen["wait"] is not None and not seen["wait"].satisfied
    # The wake is on the record, as a wake: not a gateway refusal, not an answer.
    (stray,) = [r for r in stack.audit.for_run(run_id) if r.outcome == NOT_ANSWERED]
    assert (stray.wait_id, stray.policy_decision) == (parked.awaiting_approval, "wait.unsatisfied")

    # The real answer, later, still counts: the run did not go past its question.
    assert done.commits[1] == CommitOutcome(status="committed")
    assert len(stack.registry_client.rollouts) == 1
    (record,) = rollout_audit(stack, run_id)  # the gateway saw one attempt: the real one
    assert record.outcome == "committed" and record.approval_id is not None
    assert done.asks == 1, "a stray signal does not put the question again"


def test_a_no_is_refused_at_the_act(stack: Stack, app_database: Database) -> None:
    async def refuse(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await answer(stack, env.client, **slack_click(parked, approve=False))
        return await acted(handle)

    run_id, parked, done = propose_and_wait(stack, app_database, then=refuse)

    assert done.commits == (CommitOutcome(status="refused", refusal="ApprovalRequired"),)
    assert stack.registry_client.rollouts == []
    assert len(rollout_audit(stack, run_id)) == 1
    # A1, H3: the gateway's refusal is under the agent's name; the decision is not. The
    # person's "no" is its own record, naming them, the wait and what it was bound to.
    (no,) = [r for r in stack.audit.for_run(run_id) if r.policy_decision == "human.refused"]
    wait = stack.waits.get(parked.awaiting_approval or "")
    assert wait is not None
    assert (no.principal, no.outcome, no.wait_id) == ("ana@acme", "refused", wait.wait_id)
    assert (no.action_fingerprint, no.state_snapshot) == (
        wait.action_fingerprint,
        wait.state_snapshot,
    )


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
