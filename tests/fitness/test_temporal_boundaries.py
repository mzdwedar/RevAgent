"""The durable runtime on Temporal keeps every boundary the old one had (SPEC-durable-runtime).

Temporal gives run identity, timers and replay. It does not give the reasons this
system is safe to let act, and ADR-0008 found four ways those quietly stop holding once
an orchestrator sits in front of the gateway. Each test here is one of those, asserted
against the real thing rather than argued.
"""

from __future__ import annotations

import ast
import asyncio
import configparser
import importlib.util
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from temporalio.client import Client

from agentstack.interfaces import worker_cli
from agentstack.runtime.run import RunStore
from agentstack.runtime.temporal import client as temporal_client
from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.client import start_run
from agentstack.runtime.temporal.contracts import RunEnd, RunStart, workflow_id
from agentstack.runtime.temporal.worker import build_worker
from agentstack.storage.database import Database
from tests.conftest import connect_temporal

from .conftest import TENANT, USER

ROOT = Path(__file__).resolve().parents[2]
TEMPORAL = ROOT / "src" / "agentstack" / "runtime" / "temporal"

# Criterion 38, exactly as the spec approved it.
WORKFLOW_SIDE = {"agentstack.runtime.temporal.workflows", "agentstack.runtime.temporal.contracts"}
NO_PATH_TO = {
    "agentstack.execution",
    "agentstack.storage",
    "agentstack.policy",
    "agentstack.model",
    "agentstack.prediction",
    "agentstack.runtime.temporal.activities",
}


def _contract_6() -> configparser.SectionProxy:
    config = configparser.ConfigParser()
    config.read(ROOT / ".importlinter")
    return config["importlinter:contract:6"]


def _modules(value: str) -> set[str]:
    return {line.strip() for line in value.splitlines() if line.strip()}


def test_contract_6_forbids_every_path_from_workflow_code_to_an_effect() -> None:
    contract = _contract_6()

    assert contract["type"] == "forbidden"
    assert _modules(contract["source_modules"]) == WORKFLOW_SIDE
    assert _modules(contract["forbidden_modules"]) >= NO_PATH_TO
    # Indirect imports count: a path through a helper is still a path.
    assert contract.get("allow_indirect_imports", "False") != "True"


def test_stack_guard_flags_contract_6_if_it_is_removed() -> None:
    spec = importlib.util.spec_from_file_location("stack_guard", ROOT / "scripts/stack_guard.py")
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = guard
    spec.loader.exec_module(guard)

    before = (ROOT / ".importlinter").read_text()
    after = re.sub(r"\[importlinter:contract:6\].*", "", before, flags=re.S)

    lost = guard._contract_names(before) - guard._contract_names(after)
    assert lost == {_contract_6()["name"]}


def test_workflow_code_imports_only_temporalio_its_contracts_and_the_standard_library() -> None:
    """Stricter than contract 6: a list of what may come in, not of what may not.

    A new layer added next year would not be in the contract's list, and would be in
    workflow code the day someone imports it.
    """
    allowed_roots = {"__future__", "temporalio", *sys.stdlib_module_names}
    offenders: list[str] = []
    for name in ("workflows.py", "contracts.py"):
        tree = ast.parse((TEMPORAL / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if module == "agentstack.runtime.temporal.contracts":
                    continue
                if module.split(".")[0] not in allowed_roots:
                    offenders.append(f"{name} imports {module}")
    assert not offenders, "\n".join(offenders)


def test_the_workflow_id_is_the_run_and_nothing_else() -> None:
    # Neither a session id nor a principal (spec, layer 2): one workflow per run.
    assert workflow_id("run-abc") == "experiment-run:run-abc"


# A failing activity is retried until its policy says stop, and until T35 there is no
# such policy. Every wait on a result is bounded, so a failure fails instead of hanging.
RESULT_TIMEOUT_S = 30


def _start(run_id: str, session_id: str) -> RunStart:
    return RunStart(run_id=run_id, session_id=session_id, tenant=TENANT, user=USER)


@asynccontextmanager
async def _worker(address: str, queue: str, db: Database) -> AsyncIterator[Client]:
    client = await connect_temporal(address)
    with ThreadPoolExecutor(max_workers=4) as executor:
        activities = RunActivities(runs=RunStore(db=db))
        async with build_worker(client, activities=activities, executor=executor, task_queue=queue):
            yield client


def test_a_run_records_itself_in_postgres(
    temporal_address: str, task_queue: str, app_database: Database, session_id: str
) -> None:
    run_id = f"run-{uuid.uuid4()}"

    async def run_once() -> RunEnd:
        async with _worker(temporal_address, task_queue, app_database) as client:
            handle = await start_run(client, _start(run_id, session_id), task_queue=task_queue)
            return await asyncio.wait_for(handle.result(), RESULT_TIMEOUT_S)

    assert asyncio.run(run_once()) == RunEnd(run_id=run_id, status="complete")
    run = RunStore(db=app_database).get(run_id)
    assert run is not None and run.tenant == TENANT


def test_five_concurrent_starts_are_one_workflow_and_one_runs_row(
    temporal_address: str, task_queue: str, app_database: Database, session_id: str
) -> None:
    """Criterion 29 (P1): the server decides, not a check-then-start in our code."""
    run_id = f"run-{uuid.uuid4()}"

    async def race() -> set[str | None]:
        async with _worker(temporal_address, task_queue, app_database) as client:
            handles = await asyncio.gather(
                *(
                    start_run(client, _start(run_id, session_id), task_queue=task_queue)
                    for _ in range(5)
                )
            )
            results = asyncio.gather(*(h.result() for h in handles))
            await asyncio.wait_for(results, RESULT_TIMEOUT_S)
            return {h.first_execution_run_id for h in handles}

    assert len(asyncio.run(race())) == 1
    row = app_database.fetch_one("SELECT count(*) FROM runs WHERE run_id = %s", (run_id,))
    assert row == (1,)


def test_the_worker_exits_nonzero_at_start_when_temporal_is_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Criterion 45: a worker that hangs at start looks exactly like one that works."""
    began = time.monotonic()

    code = worker_cli.main(["--address", "127.0.0.1:1"], preflight=lambda _: 0)

    assert code == 1
    assert time.monotonic() - began < temporal_client.CONNECT_TIMEOUT_S + 5
    assert "scripts/dev_up.sh" in capsys.readouterr().err


def test_the_worker_does_not_reach_for_work_when_preflight_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 18, on the new entry point: refuse before polling, not after parking."""

    async def must_not_connect(address: str, **_: object) -> Client:
        raise AssertionError(f"connected to {address} after preflight refused")

    monkeypatch.setattr(worker_cli, "connect", must_not_connect)

    assert worker_cli.main([], preflight=lambda _: 1) == 1


def test_the_worker_runs_the_real_preflight_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABPFN_TOKEN", raising=False)

    assert worker_cli.main([]) == 1
