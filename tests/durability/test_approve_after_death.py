"""Criterion 23: approved after the process that asked was killed.

This is the case the whole design is shaped around. A run parks on a human, the process
holding the prepared request and the rendered prompt is killed, and an hour later a
person clicks Approve. Nothing of the asking process remains. The approval must still
bind to what was actually shown, and the rollout must still commit exactly once.

The kill is SIGKILL, from outside, with no chance to flush anything - the same
treatment as `test_process_death.py`, because a graceful shutdown proves nothing about
the case that matters.
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

from agentstack.interfaces.wiring import Stack, answer, build_stack, deliver, envelope_for
from agentstack.observability.spans import Tracer
from agentstack.policy.approval import ApprovalRequired
from agentstack.policy.approvers import ApprovalReply
from agentstack.runtime.temporal.contracts import CommitOutcome, RunProgress, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database
from agentstack.tools.experiments import ROLLOUT, prepare_rollout
from tests.conftest import connect_temporal
from tests.durability.park_worker import ARGS, SCOPES, TENANT, USER
from tests.durability.test_slack_answer import APPROVER, slack_click
from tests.durability.test_worker_death import payload, start_worker
from tests.fitness.test_trigger_to_candidate import WATERMARK
from tests.temporal_support import DRAFT_TOOL, ROLLOUT_TOOL, progress_until
from tests.temporal_support import model_calls as asked_with

ROOT = Path(__file__).resolve().parents[2]
PARKED_TIMEOUT = 90.0


@pytest.fixture
def killed_while_parked(
    app_database: Database, app_database_url: str, checkpointer: Any, tmp_path: Path
) -> dict[str, Any]:
    """Park a run on a human approval in a worker, then kill the worker."""
    stack = build_stack(app_database, checkpointer)
    # The draft an earlier turn wrote, in the shared registry every process sees.
    stack.registry_client.commit(
        f"{TENANT}/experiments/{ARGS['experiment_id']}",
        {
            "experiment_version": ARGS["experiment_version"],
            "hypothesis": "a discount retains at-risk customers",
            "variant": "20-percent-off",
        },
    )
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    stack.approver_directory.add(
        tenant=TENANT, slack_user_id="UANA", principal="ana@acme", added_by="ops"
    )
    parked = tmp_path / "parked.txt"

    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.durability.park_worker",
            "--database-url",
            app_database_url,
            "--session-id",
            session.session_id,
            "--parked",
            str(parked),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + PARKED_TIMEOUT
        while time.monotonic() < deadline:
            if parked.exists():
                break
            if worker.poll() is not None:
                _, err = worker.communicate()
                pytest.fail(f"worker exited before parking:\n{err.decode()}")
            time.sleep(0.05)
        else:
            pytest.fail("the worker never parked on an approval")

        worker.send_signal(signal.SIGKILL)
        worker.wait(timeout=30)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)

    run_id, wait_id = parked.read_text().split()
    return {
        "run_id": run_id,
        "wait_id": wait_id,
        "session_id": session.session_id,
        "exit_code": worker.returncode,
        "worker_pid": worker.pid,
    }


def test_the_asking_process_really_died(killed_while_parked: dict[str, Any]) -> None:
    assert killed_while_parked["exit_code"] == -signal.SIGKILL


def test_the_wait_knows_what_it_was_asking_even_though_the_process_is_gone(
    killed_while_parked: dict[str, Any], app_database: Database, checkpointer: Any
) -> None:
    """The gap this task closed. The prepared request and the rendered prompt lived in
    a process that no longer exists."""
    stack = build_stack(app_database, checkpointer)

    wait = stack.waits.get(killed_while_parked["wait_id"])

    assert wait is not None
    assert wait.action_fingerprint == prepare_rollout(ARGS).fingerprint()
    assert wait.approval_summary and "IRREVERSIBLE" in wait.approval_summary


def test_a_human_approves_in_a_process_that_never_asked_and_the_rollout_commits_once(
    killed_while_parked: dict[str, Any], app_database: Database, checkpointer: Any
) -> None:
    """Criterion 23. Nothing of the asking process remains but rows in Postgres."""
    stack: Stack = build_stack(app_database, checkpointer)
    run = stack.runs.get(killed_while_parked["run_id"])
    assert run is not None

    # Before the answer, the rollout cannot commit.
    view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)
    with pytest.raises(ApprovalRequired):
        stack.deps.gateway.execute(
            request=prepare_rollout(ARGS),
            spec=ROLLOUT,
            envelope=envelope_for(view, scopes=SCOPES),
            run_id=run.run_id,
            state_snapshot="as-shown",
            tracer=tracer,
        )

    resolution = stack.coordinator.apply(
        ApprovalReply(
            run_id=run.run_id,
            wait_id=killed_while_parked["wait_id"],
            experiment_version=str(ARGS["experiment_version"]),
            data_as_of="telecom-bigml:f107d488",
            approved=True,
            slack_user_id="UANA",
            channel="C1",
        )
    )
    assert resolution.approved is True
    assert resolution.approver == "ana@acme"

    for _ in range(3):
        stack.deps.gateway.execute(
            request=prepare_rollout(ARGS),
            spec=ROLLOUT,
            envelope=envelope_for(view, scopes=SCOPES),
            run_id=run.run_id,
            state_snapshot="as-shown",
            tracer=tracer,
        )

    assert len(stack.registry_client.rollouts) == 1, "the variant went out more than once"
    assert os.getpid() != killed_while_parked["worker_pid"]


def test_the_approval_records_what_the_dead_process_had_shown(
    killed_while_parked: dict[str, Any], app_database: Database, checkpointer: Any
) -> None:
    """The summary is the approver's own evidence of what they agreed to, and it was
    rendered by a process that is gone."""
    stack = build_stack(app_database, checkpointer)
    run = stack.runs.get(killed_while_parked["run_id"])
    assert run is not None

    stack.coordinator.apply(
        ApprovalReply(
            run_id=run.run_id,
            wait_id=killed_while_parked["wait_id"],
            experiment_version=str(ARGS["experiment_version"]),
            data_as_of="telecom-bigml:f107d488",
            approved=True,
            slack_user_id="UANA",
            channel="C1",
        )
    )

    found = stack.approvals.find(
        run_id=run.run_id,
        action_fingerprint=prepare_rollout(ARGS).fingerprint(),
        state_snapshot="as-shown",
    )
    assert found is not None
    assert "IRREVERSIBLE" in found.summary
    assert found.approver == "ana@acme"


# --- the same case on Temporal (T44, criterion 31) ---
#
# Everything above drives the record directly: the wait, the approval and the gateway.
# Here the run is a Temporal workflow on a real worker process. It is killed while it
# waits for a person, the person answers through Slack's signed callback while **no
# worker is alive**, and a fresh process finishes the run from history.

# P4: the dead worker's sticky queue times out after ~10s, then the task moves.
RESUME_BOUND_S = 15.0


def test_killed_while_parked_the_run_is_finished_by_a_fresh_worker_after_a_slack_yes(
    app_database: Database,
    app_database_url: str,
    checkpointer: Any,
    temporal_address: str,
    task_queue: str,
    tmp_path: Path,
) -> None:
    stack = build_stack(app_database, checkpointer)
    stack.approver_directory.add(
        tenant=TENANT, slack_user_id=APPROVER, principal="ana@acme", added_by="t44"
    )
    scorer_calls, model_calls, asks = (
        tmp_path / "scorer-calls.txt",
        tmp_path / "model-calls.txt",
        tmp_path / "asks.txt",
    )
    recorded = ("--model-calls", str(model_calls), "--asks", str(asks))
    queue = f"{task_queue}-{uuid.uuid4().hex[:6]}"
    first = start_worker(
        temporal_address, queue, app_database_url, scorer_calls, tmp_path / "1.ready", *recorded
    )
    second: subprocess.Popen[bytes] | None = None
    try:

        async def parked_on_a_person() -> RunProgress:
            client = await connect_temporal(temporal_address)
            run_id = await deliver(stack, client, payload(WATERMARK), source="t", task_queue=queue)
            handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            return await progress_until(
                handle, lambda p: p.awaiting_approval is not None and p.asks == 1, timeout=60
            )

        parked = asyncio.run(parked_on_a_person())
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)

        async def answered_with_nobody_listening() -> None:
            # Authorised and recorded by layer 8 in this process; the signal waits in history.
            client = await connect_temporal(temporal_address)
            await answer(stack, client, **slack_click(parked))

        asyncio.run(answered_with_nobody_listening())

        second = start_worker(
            temporal_address, queue, app_database_url, scorer_calls, tmp_path / "2.ready", *recorded
        )
        began = time.monotonic()

        async def acted() -> RunProgress:
            client = await connect_temporal(temporal_address)
            handle = client.get_workflow_handle_for(
                ExperimentWorkflow.run, workflow_id(parked.run_id)
            )
            return await progress_until(handle, lambda p: len(p.commits) == 1, timeout=60)

        done = asyncio.run(acted())
        took = time.monotonic() - began
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    assert first.returncode == -signal.SIGKILL
    assert took <= RESUME_BOUND_S, f"resumed in {took:.1f}s, over the {RESUME_BOUND_S}s bound"

    # Prepared once, asked once, both by the process that died; committed once, by the other.
    assert asked_with(model_calls, tool=ROLLOUT_TOOL) == [str(first.pid)], "prepared once"
    assert asked_with(model_calls, tool=DRAFT_TOOL) == [str(first.pid)], "drafted once"
    assert asks.read_text().split("\n")[:-1] == [f"{first.pid} {parked.awaiting_approval}"]
    assert scorer_calls.read_text().split() == [str(first.pid)], "scored once"
    assert done.commits == (CommitOutcome(status="committed"),)
    assert len(stack.registry_client.rollouts) == 1
    committed = [
        r
        for r in stack.audit.for_run(parked.run_id)
        if r.resource.endswith("/rollout") and r.outcome == "committed"
    ]
    (record,) = committed
    approval = stack.approvals.find(
        run_id=parked.run_id,
        action_fingerprint=record.action_fingerprint,
    )
    assert approval is not None and approval.id == record.approval_id
    assert approval.approver == "ana@acme", "the rollout rests on the Slack answer"
    print(f"a fresh worker committed {took:.1f}s after it was ready")
