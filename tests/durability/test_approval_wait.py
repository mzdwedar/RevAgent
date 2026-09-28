"""T41: the approval wait, asked and asked again on durable timers, never expired.

A proposal is drafted (PRE_COMMIT) and then its rollout is proposed in a second turn.
The gateway stops that one at the approval boundary (ALWAYS): the turn parks a
`human_approval` wait, and the run asks a person. The question is built from the record
(the wait's summary and the frozen cohort's size and value), so any process can put it.

Unanswered, it is put again every REASK_EVERY, however long that takes: an approval
nobody answers is neither a refusal nor a consent (SPEC.md). These run on the
time-skipping server, so "72 hours" is 72 simulated hours.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.interfaces.slack import RecordingNotifier
from agentstack.interfaces.wiring import Stack, build_stack, deliver
from agentstack.runtime.drafting import ROLLOUT_PERCENTAGE, estimated_customers
from agentstack.runtime.temporal.contracts import (
    REASK_EVERY,
    ROLLOUT,
    RunProgress,
    workflow_id,
)
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.runtime.waits import APPROVAL_REASK_AFTER, HUMAN_APPROVAL, WaitStore
from agentstack.storage.database import Database
from agentstack.tools.experiments import ROLLOUT_STAGE
from tests.fitness.test_trigger_to_candidate import RULE, WATERMARK, StubScorer
from tests.temporal_support import (
    DraftingEngine,
    asker_for,
    progress_until,
    time_skipping,
    turns_for,
    worker_on,
)

pytestmark = pytest.mark.usefixtures("fixture_dataset")

PAYLOAD = {
    "kind": "data_arrival",
    "experiment_id": "exp-7",
    "data_as_of": WATERMARK,
    "tenant": "acme",
}


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


def posted(stack: Stack) -> list[Any]:
    notifier = stack.notifier
    assert isinstance(notifier, RecordingNotifier)
    return [ask for _, ask in notifier.posted]


def test_the_workflow_names_what_the_record_uses() -> None:
    assert ROLLOUT == ROLLOUT_STAGE
    assert REASK_EVERY == APPROVAL_REASK_AFTER


def propose_and_wait(
    stack: Stack, db: Database, *, then: Any = None, traces: Any = None
) -> tuple[str, RunProgress, Any]:
    """Deliver a proposing trigger on the time-skipping server, wait until the run is
    parked on its approval, then hand the environment to `then`. `traces` is where the
    worker's activities export their spans."""

    async def go() -> tuple[str, RunProgress, Any]:
        async with (
            time_skipping() as env,
            worker_on(
                env.client,
                "approvals",
                db,
                scorer=StubScorer(),
                rule=RULE,
                turns=turns_for(stack, DraftingEngine()),
                asker=asker_for(stack),
                traces=traces,
            ),
        ):
            run_id = await deliver(stack, env.client, PAYLOAD, source="t", task_queue="approvals")
            handle = env.client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            parked = await progress_until(
                handle, lambda p: p.awaiting_approval is not None and p.asks == 1
            )
            later = await then(env, handle, parked) if then is not None else None
            return run_id, parked, later

    return asyncio.run(go())


def test_a_proposal_is_drafted_then_put_to_a_person(stack: Stack, app_database: Database) -> None:
    run_id, parked, _ = propose_and_wait(stack, app_database)

    draft, rollout = parked.turns
    assert draft.receipts == 1, "drafted first, by policy"
    assert rollout.status == "awaiting_approval" and rollout.wait_id == parked.awaiting_approval
    assert stack.registry_client.rollouts == [], "nothing rolled out before anyone answered"

    wait = WaitStore(db=app_database).get(parked.awaiting_approval or "")
    assert wait is not None and wait.kind == HUMAN_APPROVAL and not wait.satisfied
    assert wait.wait_id.startswith("wait-approval-")
    assert wait.action_fingerprint and wait.approval_summary

    (ask,) = posted(stack)
    frozen = FrozenCohortStore(db=app_database).get(
        tenant="acme",
        experiment_id="exp-7",
        experiment_version=parked.cycles[0].experiment_version or "",
    )
    assert frozen is not None
    assert ask.wait_id == wait.wait_id and ask.run_id == run_id
    assert ask.summary == wait.approval_summary
    assert ask.percentage == ROLLOUT_PERCENTAGE
    assert ask.estimated_customers == estimated_customers(frozen) == 4
    assert ask.annual_value_at_risk_cents == frozen.annual_value_at_risk_cents
    assert ask.experiment_version == frozen.experiment_version


def test_unanswered_for_three_days_it_is_asked_at_every_interval(
    stack: Stack, app_database: Database
) -> None:
    """Criterion 32: 72 simulated hours, asked at 0, 24, 48 and 72. Never expired."""

    async def three_days(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await env.sleep(3 * REASK_EVERY + timedelta(minutes=1))
        return await progress_until(handle, lambda p: p.asks == parked.asks + 3, timeout=30)

    _, parked, later = propose_and_wait(stack, app_database, then=three_days)

    assert later.awaiting_approval == parked.awaiting_approval, "still waiting, same question"
    assert len(posted(stack)) == 4
    wait = WaitStore(db=app_database).get(parked.awaiting_approval or "")
    assert wait is not None and not wait.satisfied
    assert wait.reasks == 3, "each re-ask recorded on the wait, for the operator to see"


def test_an_answer_stops_the_asking(stack: Stack, app_database: Database) -> None:
    async def answer_then_wait(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await handle.signal(ExperimentWorkflow.answered, parked.awaiting_approval)
        answered = await progress_until(handle, lambda p: p.awaiting_approval is None)
        await env.sleep(2 * REASK_EVERY)
        again: RunProgress = await handle.query(ExperimentWorkflow.progress)
        assert again.asks == answered.asks
        return again

    _, parked, later = propose_and_wait(stack, app_database, then=answer_then_wait)

    assert later.answered == (parked.awaiting_approval,)
    assert len(posted(stack)) == 1, "answered: never asked again"
