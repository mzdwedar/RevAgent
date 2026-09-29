"""Checkpoint J: a real trigger drives a real cycle through a worker that is killed.

The worker is a separate process, killed with SIGKILL while the run is parked on its
trigger wait: no cleanup, no graceful shutdown, nothing flushed. The next trigger is
delivered while **no worker is alive at all**. A fresh process then picks the run up
from Temporal's history and finishes what the dead one would have done.

What must survive that: the trigger that arrived into the gap is evaluated once; the
wait it satisfied is satisfied in Postgres; the cohort scored before the kill is not
scored again; and the run is parked on its next wait, as if nothing happened.

No effect is wired. A cycle scores and claims; it does not draft, ask or roll out.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from agentstack.interfaces.wiring import Stack, build_stack, deliver
from agentstack.runtime.temporal.contracts import RunProgress, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.runtime.waits import WaitStore
from agentstack.storage.database import Database
from tests.conftest import connect_temporal
from tests.fitness.test_trigger_to_candidate import WATERMARK
from tests.temporal_support import DRAFT_TOOL, progress_until
from tests.temporal_support import model_calls as asked_with

ROOT = Path(__file__).resolve().parents[2]
READY_TIMEOUT_S = 30.0
# The dead worker's sticky queue times out after ~10s, and then the task moves (P4).
RESUME_BOUND_S = 30.0


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


def start_worker(
    address: str,
    queue: str,
    database_url: str,
    scorer_calls: Path,
    ready: Path,
    *extra: str,
) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.durability.temporal_worker",
            "--address",
            address,
            "--task-queue",
            queue,
            "--database-url",
            database_url,
            "--scorer-calls",
            str(scorer_calls),
            "--ready",
            str(ready),
            *extra,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + READY_TIMEOUT_S
    while not ready.exists():
        if process.poll() is not None or time.monotonic() > deadline:
            process.kill()
            _, err = process.communicate()
            raise AssertionError(f"worker never became ready:\n{err.decode()[-2000:]}")
        time.sleep(0.1)
    return process


def payload(data_as_of: str, kind: str = "data_arrival") -> dict[str, str]:
    return {
        "kind": kind,
        "experiment_id": "exp-7",
        "data_as_of": data_as_of,
        "tenant": "acme",
    }


def test_a_run_survives_its_worker_being_killed_while_it_waits(
    stack: Stack,
    app_database: Database,
    app_database_url: str,
    temporal_address: str,
    task_queue: str,
    tmp_path: Path,
) -> None:
    scorer_calls = tmp_path / "scorer-calls.txt"
    queue = f"{task_queue}-{uuid.uuid4().hex[:6]}"
    first = start_worker(
        temporal_address, queue, app_database_url, scorer_calls, tmp_path / "first.ready"
    )
    second: subprocess.Popen[bytes] | None = None
    try:

        async def before_the_kill() -> tuple[str, RunProgress]:
            client = await connect_temporal(temporal_address)
            # A metric movement: scored, then refused the proposal by layer 8, so the run
            # parks on its next trigger wait. (A proposal would park it on a person's
            # answer instead, and triggers wait behind that: T41.)
            first_trigger = payload(WATERMARK, kind="metric_movement")
            run_id = await deliver(stack, client, first_trigger, source="t", task_queue=queue)
            handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            progress = await progress_until(
                handle, lambda p: len(p.cycles) == 1 and p.waiting_on is not None
            )
            return run_id, progress

        run_id, parked = asyncio.run(before_the_kill())
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)

        async def into_the_gap() -> None:
            # No worker is alive. The signal lands in history and waits there.
            client = await connect_temporal(temporal_address)
            again = await deliver(
                stack, client, payload("fixture:the-next-batch"), source="t", task_queue=queue
            )
            assert again == run_id

        asyncio.run(into_the_gap())

        began = time.monotonic()
        second = start_worker(
            temporal_address, queue, app_database_url, scorer_calls, tmp_path / "second.ready"
        )

        async def after_the_kill() -> RunProgress:
            client = await connect_temporal(temporal_address)
            handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            return await progress_until(
                handle,
                lambda p: len(p.cycles) == 2 and p.waiting_on != parked.waiting_on,
                timeout=RESUME_BOUND_S,
            )

        resumed = asyncio.run(after_the_kill())
        took = time.monotonic() - began
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    assert parked.cycles[0].refusal == "OutcomeNotAuthorized"
    # The batch that arrived into the gap: evaluated once, by the new process. The
    # snapshot on disk is still the first batch, so the honest answer is to abstain.
    assert resumed.cycles[1].data_as_of == "fixture:the-next-batch"
    assert resumed.cycles[1].outcome == "abstain"

    calls = scorer_calls.read_text().split()
    assert calls == [str(first.pid)], "scored once, before the kill, and never again"

    waits = WaitStore(db=app_database)
    assert parked.waiting_on is not None
    satisfied = waits.get(parked.waiting_on)
    assert satisfied is not None and satisfied.satisfied, "the gap's trigger settled its wait"
    assert satisfied.payload["data_as_of"] == "fixture:the-next-batch"
    # Signal-with-start hands the run its first trigger as it starts, so no wait came
    # before cycle 1: wait 0 was the one the gap's trigger settled, and 1 is next.
    assert parked.waiting_on == f"wait-{run_id}-trigger-0"
    (pending,) = waits.pending_for(run_id)
    assert pending.wait_id == resumed.waiting_on == f"wait-{run_id}-trigger-1"
    assert app_database.fetch_one("SELECT count(*) FROM trigger_cycles") == (2,)
    print(f"resumed in a fresh process {took:.1f}s after it started")


# The turn heartbeats; its retry comes once HEARTBEAT_TIMEOUT (15s) passes unbeaten.
TURN_RESUME_BOUND_S = 45.0


def test_a_turn_killed_after_the_model_answered_is_finished_without_asking_again(
    stack: Stack,
    app_database_url: str,
    temporal_address: str,
    task_queue: str,
    tmp_path: Path,
) -> None:
    """T40, re-pointing criterion 1 at Temporal: killed mid-turn, resumed, not re-asked.

    The first worker is killed after the model's answer was checkpointed and before
    the gateway was called, so no effect has begun. The activity's heartbeat stops, and
    Temporal hands the turn to a fresh process. That process resumes the turn from its
    checkpoint: it does not ask the model again, and it commits the draft once.
    """
    scorer_calls = tmp_path / "scorer-calls.txt"
    model_calls = tmp_path / "model-calls.txt"
    reached = tmp_path / "reached-the-gateway.txt"
    queue = f"{task_queue}-{uuid.uuid4().hex[:6]}"
    first = start_worker(
        temporal_address,
        queue,
        app_database_url,
        scorer_calls,
        tmp_path / "first.ready",
        "--model-calls",
        str(model_calls),
        "--hang-before-gateway",
        str(reached),
    )
    second: subprocess.Popen[bytes] | None = None
    try:

        async def into_the_turn() -> str:
            client = await connect_temporal(temporal_address)
            return await deliver(stack, client, payload(WATERMARK), source="t", task_queue=queue)

        run_id = asyncio.run(into_the_turn())
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not reached.exists():
            assert time.monotonic() < deadline, "the turn never reached the gateway"
            time.sleep(0.1)
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)

        began = time.monotonic()
        second = start_worker(
            temporal_address,
            queue,
            app_database_url,
            scorer_calls,
            tmp_path / "second.ready",
            "--model-calls",
            str(model_calls),
        )

        async def finished() -> RunProgress:
            client = await connect_temporal(temporal_address)
            handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            # Settled at the approval: drafted, then the rollout proposed and parked.
            return await progress_until(
                handle, lambda p: len(p.turns) == 2, timeout=TURN_RESUME_BOUND_S
            )

        progress = asyncio.run(finished())
        took = time.monotonic() - began
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    assert reached.read_text() == "create_experiment_draft", "killed at the draft's commit"
    drafting = asked_with(model_calls, tool=DRAFT_TOOL)
    assert drafting == [str(first.pid)], "asked to draft once, before the kill"
    turn = progress.turns[0]
    assert turn.refusal is None and turn.receipts == 1, turn
    assert list(stack.registry_client.drafts) == ["acme/experiments/exp-7"]
    committed = [r for r in stack.audit.for_run(run_id) if r.outcome == "committed"]
    assert len(committed) == 1
    print(f"turn finished by a fresh process {took:.1f}s after it started")


def test_a_draft_whose_answer_died_with_its_worker_deduplicates_and_the_run_goes_on(
    stack: Stack,
    app_database: Database,
    app_database_url: str,
    temporal_address: str,
    task_queue: str,
    tmp_path: Path,
) -> None:
    """Audit finding H1: the draft commits and the ledger settles, then the worker is
    killed before the step records it. The fresh process reruns the turn from its
    checkpoint, and the rerun has to deduplicate.

    It used to park a `human_approval` wait instead. The draft had been bound to the
    registry's description of the experiment, which was nothing before the draft and
    something after it, so the rerun read its own policy grant as stale before the
    ledger was asked. The workflow saw no receipt and never asked anyone, and the
    pending wait blocked every later turn of the run for good.
    """
    scorer_calls = tmp_path / "scorer-calls.txt"
    reached = tmp_path / "past-the-gateway.txt"
    queue = f"{task_queue}-{uuid.uuid4().hex[:6]}"
    first = start_worker(
        temporal_address,
        queue,
        app_database_url,
        scorer_calls,
        tmp_path / "first.ready",
        "--hang-after-gateway",
        str(reached),
    )
    second: subprocess.Popen[bytes] | None = None
    try:

        async def into_the_turn() -> str:
            client = await connect_temporal(temporal_address)
            return await deliver(stack, client, payload(WATERMARK), source="t", task_queue=queue)

        run_id = asyncio.run(into_the_turn())
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not reached.exists():
            assert time.monotonic() < deadline, "the draft never reached the gateway"
            time.sleep(0.1)
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)
        # The window: the effect is in the registry and the ledger, and no step says so.
        assert list(stack.registry_client.drafts) == ["acme/experiments/exp-7"]
        assert stack.ledger.unresolved_keys() == ()
        assert not any(r.status == "completed" for r in stack.steps.records_for(run_id))

        second = start_worker(
            temporal_address, queue, app_database_url, scorer_calls, tmp_path / "second.ready"
        )

        async def finished() -> RunProgress:
            client = await connect_temporal(temporal_address)
            handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            return await progress_until(
                handle, lambda p: len(p.turns) == 2, timeout=TURN_RESUME_BOUND_S
            )

        progress = asyncio.run(finished())
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    assert reached.read_text() == "create_experiment_draft", "killed after the draft's commit"
    drafted, proposed = progress.turns
    assert drafted.receipts == 1 and drafted.wait_id is None, drafted
    assert proposed.status == "awaiting_approval", "the run went on to the rollout"
    (pending,) = WaitStore(db=app_database).pending_for(run_id)
    assert pending.wait_id == proposed.wait_id == progress.awaiting_approval
    assert list(stack.registry_client.drafts) == ["acme/experiments/exp-7"]
    drafts = [r for r in stack.audit.for_run(run_id) if not r.resource.endswith("/rollout")]
    assert [r.outcome for r in drafts] == ["committed", "deduplicated"]
    assert all(r.policy_decision != "approval.stale" for r in drafts)
