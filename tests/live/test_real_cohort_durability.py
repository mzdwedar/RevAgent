"""Checkpoint C, the last open box: a real cohort is scored, the worker dies mid-flight,
and the run resumes.

Every piece existed and was tested alone: the wiring (T13), recorded scores (T6),
kill-and-resume on Temporal (T44, Checkpoint J). Nothing drove the whole path with real
data. This does, on `bank-churn`: the real file from `data/`, its recorded TabPFN scores,
a trigger delivered through ingress, and a worker SIGKILLed with scoring in flight.

`bank-churn` has no observed revenue, deliberately (`context/datasets.py`), so the run
ends in an honest abstain and never reaches a proposal, an approval or a rollout. That
is the point of choosing it: the path under test is scoring, the kill and the resume,
and a refusal that survives them. The approval boundary under a kill is
`tests/durability/test_approve_after_death.py`.

Not part of the default suite: it needs the Kaggle files on disk, like the rest of
`tests/live`.

    uv run pytest tests/live/test_real_cohort_durability.py -v
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

from agentstack.context import datasets
from agentstack.interfaces.wiring import Stack, build_stack, deliver
from agentstack.runtime.temporal.contracts import RunProgress, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database
from tests.conftest import connect_temporal
from tests.durability.test_worker_death import READY_TIMEOUT_S, payload, start_worker
from tests.temporal_support import progress_until

DATASET = "bank-churn"
# The evaluation has no heartbeat, so a dead worker is noticed when the activity's
# start_to_close_timeout (120s, `workflows.CYCLE_TIMEOUT`) passes, not sooner.
RESUME_BOUND_S = 180.0
# Longer than the bound, so the replacement worker is alive when the retry arrives.
WORKER_LIFETIME_S = "300"


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


def test_a_real_cohort_scored_by_a_killed_worker_is_resumed_and_refused_honestly(
    stack: Stack,
    app_database: Database,
    app_database_url: str,
    temporal_address: str,
    task_queue: str,
    tmp_path: Path,
) -> None:
    watermark = datasets.load(DATASET).data_as_of
    assert watermark == datasets.read_manifest()[DATASET]["data_as_of"]

    scorer_calls = tmp_path / "scorer-calls.txt"
    model_calls = tmp_path / "model-calls.txt"
    queue = f"{task_queue}-{uuid.uuid4().hex[:6]}"
    first = start_worker(
        temporal_address,
        queue,
        app_database_url,
        scorer_calls,
        tmp_path / "first.ready",
        "--real-cohort",
        DATASET,
        "--hang-in-scorer",
        "--model-calls",
        str(model_calls),
        "--lifetime",
        WORKER_LIFETIME_S,
    )
    second: subprocess.Popen[bytes] | None = None
    try:

        async def into_the_evaluation() -> str:
            client = await connect_temporal(temporal_address)
            return await deliver(
                stack,
                client,
                {**payload(watermark), "experiment_id": "exp-bank"},
                source="t",
                task_queue=queue,
            )

        run_id = asyncio.run(into_the_evaluation())
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not scorer_calls.exists():
            assert time.monotonic() < deadline, "scoring never started"
            time.sleep(0.1)

        # Scoring is in flight and nothing about the cycle has been recorded as done.
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)

        began = time.monotonic()
        second = start_worker(
            temporal_address,
            queue,
            app_database_url,
            scorer_calls,
            tmp_path / "second.ready",
            "--real-cohort",
            DATASET,
            "--model-calls",
            str(model_calls),
            "--lifetime",
            WORKER_LIFETIME_S,
        )

        async def after_the_kill() -> RunProgress:
            client = await connect_temporal(temporal_address)
            handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            return await progress_until(
                handle,
                lambda p: len(p.cycles) == 1 and p.waiting_on is not None,
                timeout=RESUME_BOUND_S,
            )

        resumed = asyncio.run(after_the_kill())
        took = time.monotonic() - began
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    (cycle,) = resumed.cycles
    assert cycle.data_as_of == watermark
    assert cycle.outcome == "abstain", "bank-churn has no observed revenue to target on"
    assert cycle.refusal is None

    # The dead worker started scoring and never finished; the new one scored once and
    # the cycle settled. Two starts, one result: the retry is the resume.
    assert second is not None
    assert scorer_calls.read_text().split() == [str(first.pid), str(second.pid)]

    # Nothing past the refusal ran: no model call, no turn, no question, no act.
    assert not model_calls.exists()
    assert resumed.turns == () and resumed.awaiting_approval is None and resumed.commits == ()

    assert app_database.fetch_one("SELECT count(*) FROM trigger_cycles") == (1,)
    (settled,) = app_database.fetch_all("SELECT outcome FROM trigger_cycles")
    assert settled == ("abstain",)
    print(f"resumed in a fresh process {took:.1f}s after it started")
