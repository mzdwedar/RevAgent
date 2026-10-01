"""T40: the turn as one activity, and the first side effect from inside Temporal.

A `propose` cycle hands the run to a drafting turn: the LangGraph graph, run whole
inside one activity, against the Postgres checkpointer. What the turn commits (the
registry draft, PRE_COMMIT) goes through the gateway with the tool's content key.

What this has to show (Checkpoint K, spec criterion 36):

* the draft lands once and is audited with the policy rule that let it;
* the instruction and the envelope never reach Temporal's history;
* a retry after the turn finished, a redelivery after the run closed, and a workflow
  reset each find the turn already taken: the model isn't asked again and the
  registry is written once.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import EventType
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import Client

from agentstack.interfaces.wiring import Stack, build_stack, deliver
from agentstack.runtime.temporal.contracts import (
    DRAFT,
    RunProgress,
    Trigger,
    TurnIntent,
    workflow_id,
)
from agentstack.runtime.temporal.workflows import HEARTBEAT_TIMEOUT, ExperimentWorkflow
from agentstack.storage.database import Database
from agentstack.tools.experiments import DRAFT_STAGE
from tests.fitness.test_trigger_to_candidate import RULE, WATERMARK, StubScorer
from tests.temporal_support import (
    DRAFT_TOOL,
    DraftingEngine,
    activities_for,
    progress_until,
    running_worker,
    turns_for,
)

pytestmark = pytest.mark.usefixtures("fixture_dataset")

PAYLOAD = {
    "kind": "data_arrival",
    "experiment_id": "exp-7",
    "data_as_of": WATERMARK,
    "tenant": "acme",
}
DRAFTED = "acme/experiments/exp-7"


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


def handle_of(client: Client, run_id: str) -> Any:
    return client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))


def worker(stack: Stack, db: Database, address: str, queue: str, engine: DraftingEngine) -> Any:
    return running_worker(
        address, queue, db, scorer=StubScorer(), rule=RULE, turns=turns_for(stack, engine)
    )


def test_the_workflow_names_the_draft_stage_the_tools_define() -> None:
    assert DRAFT == DRAFT_STAGE


def test_a_proposed_cohort_is_drafted_once_and_audited_with_its_rule(
    stack: Stack, app_database: Database, temporal_address: str, task_queue: str
) -> None:
    engine = DraftingEngine()

    async def draft() -> tuple[str, RunProgress, str]:
        async with worker(stack, app_database, temporal_address, task_queue, engine) as client:
            run_id = await deliver(stack, client, PAYLOAD, source="t", task_queue=task_queue)
            handle = handle_of(client, run_id)
            progress = await progress_until(handle, lambda p: len(p.turns) == 2)
            history = (await handle.fetch_history()).to_json()
            return run_id, progress, history

    run_id, progress, history = asyncio.run(draft())

    turn = progress.turns[0]  # the draft; the second is the rollout, parked for a person
    assert turn.refusal is None and turn.receipts == 1, turn
    assert engine.drafts == 1
    draft_row = stack.registry_client.drafts[DRAFTED]
    assert draft_row["experiment_version"] == progress.cycles[0].experiment_version
    committed = [r for r in stack.audit.for_run(run_id) if r.outcome == "committed"]
    assert len(committed) == 1
    assert committed[0].policy_decision.startswith("approval.policy:"), committed[0]

    # Checkpoint K's spot check (T48 makes it exhaustive): the instruction the model
    # read and the envelope it acted under are nowhere in the recorded history.
    assert "create_experiment_draft" not in history
    assert "vault://" not in history
    assert "experiments:draft" not in history


def test_the_turn_is_told_its_ids_and_the_transcript_keeps_what_was_said(
    stack: Stack, app_database: Database, temporal_address: str, task_queue: str
) -> None:
    async def draft() -> str:
        async with worker(
            stack, app_database, temporal_address, task_queue, DraftingEngine()
        ) as client:
            run_id = await deliver(stack, client, PAYLOAD, source="t", task_queue=task_queue)
            await progress_until(handle_of(client, run_id), lambda p: len(p.turns) == 2)
            return run_id

    run_id = asyncio.run(draft())

    run = stack.runs.get(run_id)
    assert run is not None
    events = stack.transcripts.for_session(run.session_id)
    asked = [e.body for e in events if e.kind == "user"]
    drafting, proposing = asked  # two turns: the draft, then the rollout's proposal
    assert "create_experiment_draft" in drafting and "experiment_id=exp-7" in drafting
    assert "roll_out_variant_to_percentage" in proposing and "percentage=10" in proposing
    assert any(e.kind == "agent" for e in events)


def test_a_rerun_of_a_finished_turn_asks_the_model_nothing(
    stack: Stack, app_database: Database
) -> None:
    """A completion lost after the turn finished: Temporal runs the activity again."""
    engine = DraftingEngine()
    run_start = _run_for(stack)
    activities = activities_for(
        app_database, scorer=StubScorer(), rule=RULE, turns=turns_for(stack, engine)
    )
    activities.evaluate_cycle(
        Trigger(kind="data_arrival", experiment_id="exp-7", data_as_of=WATERMARK, tenant="acme"),
        run_start,
    )
    intent = TurnIntent(
        run_id=run_start,
        stage=DRAFT,
        experiment_id="exp-7",
        data_as_of=WATERMARK,
        kind="data_arrival",
    )

    first = activities.run_turn(intent)
    second = activities.run_turn(intent)

    assert first == second
    assert engine.drafts == 1
    assert list(stack.registry_client.drafts) == [DRAFTED]


def test_a_redelivery_after_the_run_closed_finds_the_turn_taken(
    stack: Stack, app_database: Database, temporal_address: str, task_queue: str
) -> None:
    engine = DraftingEngine()

    async def deliver_twice_around_a_close() -> RunProgress:
        async with worker(stack, app_database, temporal_address, task_queue, engine) as client:
            run_id = await deliver(stack, client, PAYLOAD, source="t", task_queue=task_queue)
            await progress_until(handle_of(client, run_id), lambda p: len(p.turns) == 2)
            await handle_of(client, run_id).terminate("closed")
            await deliver(stack, client, PAYLOAD, source="t", task_queue=task_queue)
            return await progress_until(handle_of(client, run_id), lambda p: len(p.turns) == 2)

    progress = asyncio.run(deliver_twice_around_a_close())

    assert progress.turns[0].receipts == 1
    assert engine.drafts == 1, "the second execution found the turn already taken"
    assert list(stack.registry_client.drafts) == [DRAFTED]


def test_a_workflow_reset_replays_the_turn_without_a_second_draft(
    stack: Stack, app_database: Database, temporal_address: str, task_queue: str
) -> None:
    """Reset re-executes every activity after the chosen point, the turn included (E1)."""
    engine = DraftingEngine()

    async def draft_then_reset() -> tuple[str | None, str | None, RunProgress]:
        async with worker(stack, app_database, temporal_address, task_queue, engine) as client:
            run_id = await deliver(stack, client, PAYLOAD, source="t", task_queue=task_queue)
            handle = handle_of(client, run_id)
            await progress_until(handle, lambda p: len(p.turns) == 2)
            before = (await handle.describe()).run_id

            # Reset to the end of the first workflow task: before the cycle and the turn.
            completed = [
                e.event_id
                async for e in handle.fetch_history_events()
                if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
            ]
            first_task = completed[0]
            reset = await client.workflow_service.reset_workflow_execution(
                ResetWorkflowExecutionRequest(
                    namespace=client.namespace,
                    workflow_execution=WorkflowExecution(workflow_id=handle.id, run_id=before),
                    reason="T40: a reset must not draft twice",
                    workflow_task_finish_event_id=first_task,
                    request_id=str(uuid.uuid4()),
                )
            )
            after = handle_of(client, run_id)
            progress = await progress_until(after, lambda p: len(p.turns) == 2)
            return before, reset.run_id, progress

    before, after, progress = asyncio.run(draft_then_reset())

    assert before != after, "the reset started a new execution"
    assert progress.turns[0].receipts == 1
    assert engine.drafts == 1
    assert list(stack.registry_client.drafts) == [DRAFTED]


class SlowDraftingEngine(DraftingEngine):
    """A model that takes longer to answer than the turn's heartbeat timeout."""

    def generate(self, request: Any) -> Any:
        if DRAFT_TOOL in request.tool_names:  # the turn under test; the rollout stays quick
            time.sleep(SLOW_MODEL_S)
        return super().generate(request)


SLOW_MODEL_S = HEARTBEAT_TIMEOUT.total_seconds() + 5


def test_a_turn_longer_than_its_heartbeat_timeout_is_not_retried_while_alive(
    stack: Stack, app_database: Database, temporal_address: str, task_queue: str
) -> None:
    """The heartbeat is what separates a slow turn from a dead worker.

    Without it, a turn that simply takes longer than HEARTBEAT_TIMEOUT (a real model
    may take a minute) would be declared dead and started again while the first
    attempt is still running: two turns, two model calls, racing to one commit.
    """
    engine = SlowDraftingEngine()

    async def slow_draft() -> RunProgress:
        async with worker(stack, app_database, temporal_address, task_queue, engine) as client:
            run_id = await deliver(stack, client, PAYLOAD, source="t", task_queue=task_queue)
            return await progress_until(
                handle_of(client, run_id), lambda p: len(p.turns) == 2, timeout=SLOW_MODEL_S + 30
            )

    progress = asyncio.run(slow_draft())

    assert progress.turns[0].receipts == 1
    assert engine.drafts == 1, "a second attempt started while the first was still alive"


def _run_for(stack: Stack) -> str:
    from agentstack.interfaces.triggers import parse_trigger
    from agentstack.interfaces.wiring import run_for_trigger

    return run_for_trigger(stack, parse_trigger(PAYLOAD, source="t")).run_id
