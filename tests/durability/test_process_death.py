"""Criterion 1: a parked run resumes from a checkpoint rebuilt in a fresh process.

A resume inside the process that wrote the checkpoint proves almost nothing - the
objects are still in memory and a checkpointer that never wrote a row would pass. So a
real process is started, allowed to get past the model call, and killed with SIGKILL.
It gets no chance to flush anything, run an atexit hook, or close a pool.

What must survive that: the turn resumes, it finishes, and it does not ask the model
again. The last is the point. The model call is the expensive, non-deterministic step;
a "resume" that repeats it is continuing from an answer the run never actually received.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentstack.interfaces.wiring import build_stack, envelope_for
from agentstack.runtime.loop import run_turn
from agentstack.runtime.run import Run, new_run
from agentstack.storage.database import Database
from agentstack.tools.experiments import ROLLOUT_STAGE
from tests.durability.worker import MESSAGE, SCOPES, RecordingEngine

ROOT = Path(__file__).resolve().parents[2]
REACHED_TIMEOUT = 90.0


@pytest.fixture
def parked(
    app_database: Database, app_database_url: str, checkpointer: Any, tmp_path: Path
) -> dict[str, Any]:
    """Start a worker, let it get inside `act`, then kill it dead."""
    stack = build_stack(app_database, checkpointer)
    session = stack.resolver.start(user_id="agent-operator", tenant="acme")
    run = stack.runs.ensure(
        new_run(
            session_id=session.session_id,
            tenant="acme",
            user="agent-operator",
            stage=ROLLOUT_STAGE,
            channel="durability",
        )
    )
    stack.registry_client.commit(
        "acme/experiments/exp-7",
        {"experiment_version": "exp:v1", "hypothesis": "a discount retains", "variant": "20-off"},
    )
    model_calls = tmp_path / "model-calls.txt"
    reached = tmp_path / "reached.txt"

    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.durability.worker",
            "--database-url",
            app_database_url,
            "--run-id",
            run.run_id,
            "--session-id",
            session.session_id,
            "--turn-id",
            "killed",
            "--model-calls",
            str(model_calls),
            "--reached",
            str(reached),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + REACHED_TIMEOUT
        while time.monotonic() < deadline:
            if reached.exists():
                break
            if worker.poll() is not None:
                _, err = worker.communicate()
                pytest.fail(f"worker exited before reaching the surface:\n{err.decode()}")
            time.sleep(0.05)
        else:
            pytest.fail("worker never reached the surface")

        # SIGKILL, not terminate: the run must survive a process that got no warning.
        worker.send_signal(signal.SIGKILL)
        worker.wait(timeout=30)
    finally:
        # Belt and braces: a worker that outlived its test would hold a connection
        # and hang the next one. No coverage pragma - coverage measures src/, and
        # a suppression in a test is a suppression.
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)

    return {
        "run": run,
        "session_id": session.session_id,
        "worker_pid": worker.pid,
        "exit_code": worker.returncode,
        "model_calls": model_calls,
        "reached": reached,
    }


def test_the_worker_really_died(parked: dict[str, Any]) -> None:
    """If it exited cleanly the rest of this file proves nothing."""
    assert parked["exit_code"] == -signal.SIGKILL
    assert parked["model_calls"].read_text().strip(), "the worker died before the model call"


def test_it_died_inside_the_surface_read_it_was_supposed_to(parked: dict[str, Any]) -> None:
    pid, resource, _ = parked["reached"].read_text().split(maxsplit=2)

    assert int(pid) == parked["worker_pid"]
    assert resource == "acme/experiments/exp-7"


def test_the_checkpoint_outlived_the_process_that_wrote_it(
    parked: dict[str, Any], app_database: Database
) -> None:
    rows = app_database.fetch_all(
        "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = %s",
        (f"{parked['run'].run_id}:killed",),
    )

    assert rows[0][0] > 0, "a killed process left nothing to resume from"


def test_a_fresh_process_resumes_the_turn_without_asking_the_model_again(
    parked: dict[str, Any], app_database: Database, checkpointer: Any
) -> None:
    """The acceptance. Nothing of the dead worker remains except rows in Postgres."""
    before = parked["model_calls"].read_text().splitlines()
    assert len(before) == 1
    assert int(before[0]) == parked["worker_pid"]

    stack = build_stack(app_database, checkpointer)
    stack.deps.engine = RecordingEngine(stack.deps.engine, parked["model_calls"])
    run: Run = parked["run"]
    view = stack.resolver.resolve(
        session_id=parked["session_id"], user_id="agent-operator", tenant="acme"
    )

    result = run_turn(
        run=run,
        envelope=envelope_for(view, scopes=SCOPES),
        message=MESSAGE,
        deps=stack.deps,
        turn_id="killed",
    )

    after = parked["model_calls"].read_text().splitlines()
    assert result.status == "complete"
    assert after == before, (
        f"the model was called again after the resume: {after}. The turn continued from "
        "an answer it never received."
    )
    assert os.getpid() != parked["worker_pid"], "this is the process that wrote it"


def test_the_resumed_turn_actually_reached_the_surface(
    parked: dict[str, Any], app_database: Database, checkpointer: Any
) -> None:
    """Resuming without re-calling the model must not mean resuming without doing the
    work. A turn that skipped `act` would satisfy the previous test and be useless."""
    stack = build_stack(app_database, checkpointer)
    stack.deps.engine = RecordingEngine(stack.deps.engine, parked["model_calls"])
    view = stack.resolver.resolve(
        session_id=parked["session_id"], user_id="agent-operator", tenant="acme"
    )

    result = run_turn(
        run=parked["run"],
        envelope=envelope_for(view, scopes=SCOPES),
        message=MESSAGE,
        deps=stack.deps,
        turn_id="killed",
    )

    assert result.status == "complete"
    assert "execution.read" in result.tracer.names(), (
        "the resumed turn never reached the surface the worker hung on"
    )
