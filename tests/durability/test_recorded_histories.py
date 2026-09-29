"""T47: the runs whose histories the replay guard replays, recorded on purpose.

`scripts/replay_guard.py` replays `tests/fixtures/histories/*.json` against this code,
so a change to `ExperimentWorkflow` that a live run could not survive fails the build
(criterion 41). A history is only as good as the path it took, so each scenario here
drives one real path through the production worker, on the time-skipping server, and
checks it got where it was meant to before its history is kept.

Every run of the suite replays each fresh history against this code. Only
`uv run python scripts/replay_guard.py --record` writes them: a fixture changes when
someone means it to.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from agentstack.interfaces.wiring import Stack, answer, deliver
from agentstack.runtime.temporal.client import notify_answer
from agentstack.runtime.temporal.contracts import (
    NOT_ANSWERED,
    REASK_EVERY,
    UNRESOLVED,
    CommitOutcome,
    RunProgress,
    Trigger,
    workflow_id,
)
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.runtime.waits import ResumeEvent, WaitStore, resume
from agentstack.storage.database import Database
from agentstack.tools.spec import Surface
from tests.durability.test_approval_wait import PAYLOAD, propose_and_wait
from tests.durability.test_approval_wait import stack as stack  # the same fixture
from tests.durability.test_commit import LosesTheFirstRolloutAnswer, acted
from tests.durability.test_reconcile import LosesTheFirstDraftAnswer, drive, reconcile
from tests.durability.test_slack_answer import APPROVER, slack_click
from tests.fitness.test_trigger_to_candidate import RULE, WATERMARK, StubScorer
from tests.temporal_support import keep_history, progress_until, time_skipping, worker_on

pytestmark = pytest.mark.usefixtures("fixture_dataset")

# The trigger wait's deadline in the trigger scenario: short, so the timer fires.
DEADLINE = timedelta(hours=1)


@pytest.fixture(autouse=True)
def _approver(stack: Stack) -> None:
    stack.approver_directory.add(
        tenant="acme", slack_user_id=APPROVER, principal="ana@acme", added_by="t47"
    )


def test_trigger_cycles_abstain_go_overdue_and_refuse(stack: Stack, app_database: Database) -> None:
    """The run's loop with no act in it: a cycle that abstains, a trigger wait whose
    deadline passes, a late `metric_movement` that layer 8 refuses, and the run parked
    again."""

    async def go() -> RunProgress:
        async with (
            time_skipping() as env,
            worker_on(
                env.client,
                "history-triggers",
                app_database,
                scorer=StubScorer(),
                rule=RULE,
                trigger_deadline=DEADLINE,
            ),
        ):
            # A batch other than the one on disk: the cycle abstains rather than score it.
            moved = PAYLOAD | {"data_as_of": "fixture:someone-elses-batch"}
            run_id = await deliver(
                stack, env.client, moved, source="t47", task_queue="history-triggers"
            )
            handle = env.client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            await progress_until(handle, lambda p: len(p.cycles) == 1 and p.waiting_on is not None)
            await env.sleep(DEADLINE + timedelta(minutes=1))
            await progress_until(handle, lambda p: p.overdue)
            await handle.signal(
                ExperimentWorkflow.trigger,
                Trigger(
                    kind="metric_movement",
                    experiment_id="exp-7",
                    data_as_of=WATERMARK,
                    tenant="acme",
                    source="t47",
                ),
            )
            done = await progress_until(
                handle, lambda p: len(p.cycles) == 2 and p.waiting_on is not None
            )
            await keep_history(handle, "trigger_cycles")
            return done

    done = asyncio.run(go())

    abstained, refused = done.cycles
    assert abstained.outcome == "abstain"
    assert refused.refusal == "OutcomeNotAuthorized"
    assert not done.overdue, "parked again on a fresh wait"


def test_proposed_reasked_approved_and_committed(stack: Stack, app_database: Database) -> None:
    """SPEC.md's whole path: propose, draft, propose the rollout, park, ask, go a day
    unanswered and ask again, a person approves in Slack, the rollout commits."""

    async def reask_then_approve(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await env.sleep(REASK_EVERY + timedelta(minutes=1))
        await progress_until(handle, lambda p: p.asks == 2)
        await answer(stack, env.client, **slack_click(parked))
        done = await acted(handle)
        await keep_history(handle, "approved_commit")
        return done

    _, _, done = propose_and_wait(stack, app_database, then=reask_then_approve)

    assert done.asks == 2
    assert done.commits == (CommitOutcome(status="committed"),)
    assert len(stack.registry_client.rollouts) == 1


def test_a_stray_wake_waits_again_and_a_no_is_refused_at_the_act(
    stack: Stack, app_database: Database
) -> None:
    """Both of the act's other branches. Woken with no answer recorded, the act finds the
    wait unanswered and the run goes back to waiting (A1, H2); then a person says no, and
    the gateway refuses the act and the run records it."""

    async def stray_then_no(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await notify_answer(
            env.client, run_id=parked.run_id, wait_id=parked.awaiting_approval or ""
        )
        await progress_until(
            handle, lambda p: len(p.commits) == 1 and p.awaiting_approval is not None
        )
        await answer(stack, env.client, **slack_click(parked, approve=False))
        done = await acted(handle, 2)
        await keep_history(handle, "refused_at_the_act")
        return done

    _, parked, done = propose_and_wait(stack, app_database, then=stray_then_no)

    assert done.commits == (
        CommitOutcome(status=NOT_ANSWERED, wait_id=parked.awaiting_approval),
        CommitOutcome(status="refused", refusal="ApprovalRequired"),
    )
    assert stack.registry_client.rollouts == []


def test_an_unresolved_rollout_reconciled_then_deduplicated(
    stack: Stack, app_database: Database
) -> None:
    """The answer from the surface is lost: the run parks a reconcile wait, is woken
    once the claim is settled, acts again, and the ledger deduplicates."""
    stack.deps.gateway.surfaces[Surface.REGISTRY] = LosesTheFirstRolloutAnswer(
        stack.deps.gateway.surfaces[Surface.REGISTRY]
    )

    async def approve_and_reconcile(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await answer(stack, env.client, **slack_click(parked))
        unresolved = await progress_until(handle, lambda p: p.reconciling is not None)
        (key,) = stack.ledger.unresolved_keys()
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
        done = await acted(handle, 2)
        await keep_history(handle, "unresolved_reconciled")
        return done

    _, _, done = propose_and_wait(stack, app_database, then=approve_and_reconcile)

    unresolved, deduplicated = done.commits
    assert unresolved.status == "unresolved"
    assert deduplicated == CommitOutcome(status="deduplicated")
    assert done.reconciling is None


def test_a_draft_unresolved_in_its_turn_reconciled_then_taken_again(
    stack: Stack, app_database: Database
) -> None:
    """M1's path through the workflow: the drafting turn comes back unresolved, the run
    waits on its reconcile wait, `operator reconcile` settles it and wakes the run, and
    the same turn is taken again before the rollout is proposed."""
    surface = LosesTheFirstDraftAnswer(stack.deps.gateway.surfaces[Surface.REGISTRY])
    stack.deps.gateway.surfaces[Surface.REGISTRY] = surface

    async def settle_and_keep(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        assert surface.receipt is not None
        code = await reconcile(
            env.client, app_database, parked.reconciling or "", receipt=surface.receipt
        )
        assert code == 0
        done = await progress_until(handle, lambda p: p.awaiting_approval is not None)
        await keep_history(handle, "draft_unresolved_reconciled")
        return done

    _, _, done = drive(stack, app_database, settle_and_keep)

    unresolved, drafted, proposed = done.turns
    assert unresolved.status == UNRESOLVED
    assert drafted.receipts == 1 and proposed.status == "awaiting_approval"
