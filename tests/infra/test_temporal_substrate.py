"""The Temporal substrate (T32; SPEC-durable-runtime criterion 45 and its coverage risk).

Like Postgres, the orchestrator is a dependency the suite *fails* without rather than
skips: a conditional skip would trip the floor, and loosening the floor for it is the
move `stack_guard` exists to catch.

Synchronous tests, each driving its own event loop: the suite has no async plugin, and
adding one is a dependency decision this task does not need to make.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from tests.conftest import TemporalUnavailable, connect_temporal
from tests.infra.temporal_probe import Echo, echo_worker


def test_an_unreachable_server_fails_loudly_and_names_the_fix() -> None:
    with pytest.raises(TemporalUnavailable, match="scripts/dev_up.sh"):
        asyncio.run(connect_temporal("127.0.0.1:1"))


def test_a_workflow_runs_on_the_dev_server(temporal_address: str, task_queue: str) -> None:
    async def round_trip() -> str:
        client = await connect_temporal(temporal_address)
        async with echo_worker(client, task_queue):
            return await client.execute_workflow(
                Echo.run, "ping", id=f"substrate-{uuid.uuid4()}", task_queue=task_queue
            )

    assert asyncio.run(round_trip()) == "ping"


def test_every_test_gets_a_task_queue_no_other_run_shares(
    task_queue: str, temporal_run_token: str, request: pytest.FixtureRequest
) -> None:
    # Two suites on one server - two sessions, or CI and a laptop - must never hand
    # each other's workflows to the wrong worker. Postgres taught this at T22.
    assert temporal_run_token in task_queue
    assert request.node.name in task_queue


# The probe package is written fresh into a temp dir so the measurement is about the
# mechanism - sandbox re-import plus coverage tracing - and nothing else.
_PROBE_WORKFLOW = """
from temporalio import workflow


@workflow.defn
class Probe:
    @workflow.run
    async def run(self, n: int) -> int:
        doubled = n * 2  # MARK:inside-the-sandbox
        return doubled + 1  # MARK:still-inside
"""

_PROBE_RUNNER = """
import asyncio, sys, uuid
from temporalio.client import Client
from temporalio.worker import Worker
from probe_pkg.wf import Probe


async def main(address):
    client = await Client.connect(address)
    queue = f"coverage-probe-{uuid.uuid4()}"
    async with Worker(client, task_queue=queue, workflows=[Probe]):
        assert await client.execute_workflow(Probe.run, 20, id=queue, task_queue=queue) == 41

asyncio.run(main(sys.argv[1]))
"""


def test_coverage_sees_code_that_runs_inside_the_workflow_sandbox(
    tmp_path: Path, temporal_address: str
) -> None:
    """The risk the plan put first: if this fails, every workflow line reads as uncovered
    and the 98% ratchet either drops or gets gamed. Excluding the module is not a fix."""
    pkg = tmp_path / "probe_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "wf.py").write_text(textwrap.dedent(_PROBE_WORKFLOW))
    (tmp_path / "runner.py").write_text(textwrap.dedent(_PROBE_RUNNER))

    env = os.environ | {"PYTHONPATH": str(tmp_path)}
    run = [sys.executable, "-m", "coverage", "run", "--source=probe_pkg", "runner.py"]
    subprocess.run([*run, temporal_address], cwd=tmp_path, env=env, check=True, timeout=60)
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "cov.json"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
    )

    report = json.loads((tmp_path / "cov.json").read_text())
    (measured,) = [f for f in report["files"] if f.endswith("wf.py")]
    executed = set(report["files"][measured]["executed_lines"])
    source = (pkg / "wf.py").read_text().splitlines()
    marked = {i for i, line in enumerate(source, start=1) if "# MARK:" in line}
    assert marked and marked <= executed, (
        f"coverage did not record lines {sorted(marked - executed)} that ran inside "
        "the workflow sandbox"
    )
