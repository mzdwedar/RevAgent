"""L5b: a worker killed in the middle of measuring leaves one conclusion.

A real worker process takes the run past its rollout and through the outcome window (a
short one: the workflow shares this process's `contracts`, patched), and is SIGKILLed
inside the conclude activity. Nothing was recorded. A second process takes the activity
when the first attempt times out, measures, and the run goes on to act on the answer.

The attempt that was killed is only found out after the activity's 120s timeout, so this
test takes about two minutes. That is Temporal noticing a dead worker, not slack.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from agentstack.interfaces.wiring import Stack, build_stack, deliver
from agentstack.runtime.temporal.contracts import RunProgress, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database
from tests.conftest import connect_temporal
from tests.durability.test_conclude_timer import approve
from tests.durability.test_worker_death import payload, start_worker
from tests.fitness.test_trigger_to_candidate import WATERMARK
from tests.temporal_support import progress_until

ATTEMPT_TIMEOUT_S = 120.0
FINISH_BOUND_S = ATTEMPT_TIMEOUT_S + 60.0


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


def test_a_worker_killed_mid_measurement_leaves_one_conclusion_and_the_run_acts_on_it(
    stack: Stack,
    app_database: Database,
    app_database_url: str,
    temporal_address: str,
    task_queue: str,
    tmp_path: Path,
) -> None:
    scorer_calls, conclude_calls, reached = (
        tmp_path / "scorer.txt",
        tmp_path / "conclude-calls.txt",
        tmp_path / "in-conclude.txt",
    )
    queue = f"{task_queue}-{uuid.uuid4().hex[:6]}"
    first = start_worker(
        temporal_address, queue, app_database_url, scorer_calls, tmp_path / "1.ready",
        "--conclude-calls", str(conclude_calls), "--hang-in-conclude", str(reached),
    )  # fmt: skip
    second: subprocess.Popen[bytes] | None = None
    try:

        async def through_the_rollout() -> str:
            client = await connect_temporal(temporal_address)
            run_id = await deliver(stack, client, payload(WATERMARK), source="t", task_queue=queue)
            handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            parked = await progress_until(
                handle, lambda p: p.awaiting_approval is not None and p.asks == 1, timeout=60
            )
            approve(stack, parked)
            await handle.signal(ExperimentWorkflow.answered, parked.awaiting_approval)
            await progress_until(handle, lambda p: len(p.commits) == 1, timeout=60)
            return run_id

        run_id = asyncio.run(through_the_rollout())

        deadline = time.monotonic() + 30
        while not reached.exists():
            assert time.monotonic() < deadline, "the first worker never reached the measurement"
            time.sleep(0.1)
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)
        assert conclusions(app_database) == [], "killed before anything was recorded"

        second = start_worker(
            temporal_address, queue, app_database_url, scorer_calls, tmp_path / "2.ready",
            "--conclude-calls", str(conclude_calls), "--lifetime", str(FINISH_BOUND_S + 30),
        )  # fmt: skip

        async def finished() -> RunProgress:
            client = await connect_temporal(temporal_address)
            handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            # Rollout and draft turns, then the halt the conclusion asked for.
            return await progress_until(
                handle,
                lambda p: bool(p.conclusions) and len(p.turns) == 3,
                timeout=FINISH_BOUND_S,
            )

        done = asyncio.run(finished())
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()

    assert [c.action for c in done.conclusions] == ["stop"]
    assert conclusions(app_database) == [(0, "stop")], "one conclusion, however many attempts"
    pids = conclude_calls.read_text().split()
    assert len(pids) == 2 and pids[0] != pids[1], "measured by the dead worker, then by a live one"
    kinds = [str(r[0]) for r in app_database.fetch_all("SELECT kind FROM registry_events")]
    assert kinds.count("halt") == 1, "and acted on once"


def conclusions(db: Database) -> list[tuple[int, str]]:
    rows = db.fetch_all("SELECT endpoint, action FROM conclusions ORDER BY endpoint")
    return [(int(r[0]), str(r[1])) for r in rows]
