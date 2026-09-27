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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from agentstack.execution.gateway import UnresolvedEffect
from agentstack.execution.surfaces import SandboxViolation, SurfaceRefused
from agentstack.interfaces import worker_cli
from agentstack.policy.approval import ApprovalRequired, ApprovalStale
from agentstack.policy.approvers import ApproverNotAuthorized
from agentstack.policy.decisions import PolicyDenied
from agentstack.policy.triggers import OutcomeNotAuthorized
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.run import RunStore
from agentstack.runtime.temporal import client as temporal_client
from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.client import start_run
from agentstack.runtime.temporal.contracts import RunProgress, RunStart, workflow_id
from agentstack.runtime.temporal.interceptors import DECLARED, DeclaredActivitiesOnly
from agentstack.runtime.temporal.retry import DETERMINISTIC, REFUSALS, RETRY, UNDECLARED
from agentstack.runtime.temporal.worker import build_worker
from agentstack.storage.database import Database, IntegrityViolation
from tests.conftest import connect_temporal
from tests.temporal_support import NoScoring, progress_until, running_worker

from .conftest import TENANT, USER
from .temporal_probes import CallOnce

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


def test_workflow_code_imports_only_temporalio_its_own_side_and_the_standard_library() -> None:
    """Stricter than contract 6: a list of what may come in, not of what may not.

    A new layer added next year would not be in the contract's list, and would be in
    workflow code the day someone imports it.
    """
    allowed_roots = {"__future__", "temporalio", *sys.stdlib_module_names}
    # The workflow side, each file held to this same list.
    own_side = {"workflows.py", "contracts.py", "retry.py"}
    offenders: list[str] = []
    for name in sorted(own_side):
        tree = ast.parse((TEMPORAL / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if f"{module.removeprefix('agentstack.runtime.temporal.')}.py" in own_side:
                    continue
                if module.split(".")[0] not in allowed_roots:
                    offenders.append(f"{name} imports {module}")
    assert not offenders, "\n".join(offenders)


def test_the_workflow_id_is_the_run_and_nothing_else() -> None:
    # Neither a session id nor a principal (spec, layer 2): one workflow per run.
    assert workflow_id("run-abc") == "experiment-run:run-abc"


# A failing activity is retried until its policy says stop, and a regression in the
# policy is exactly what some of these tests catch. Every wait on a result is bounded,
# so a failure fails instead of hanging.
RESULT_TIMEOUT_S = 30


def _start(run_id: str, session_id: str) -> RunStart:
    return RunStart(run_id=run_id, session_id=session_id, tenant=TENANT, user=USER)


def test_a_run_records_itself_in_postgres(
    temporal_address: str, task_queue: str, app_database: Database, session_id: str
) -> None:
    run_id = f"run-{uuid.uuid4()}"

    async def run_once() -> RunProgress:
        async with running_worker(temporal_address, task_queue, app_database) as client:
            handle = await start_run(client, _start(run_id, session_id), task_queue=task_queue)
            return await progress_until(handle, lambda p: p.recorded)

    assert asyncio.run(run_once()).run_id == run_id
    run = RunStore(db=app_database).get(run_id)
    assert run is not None and run.tenant == TENANT


def test_five_concurrent_starts_are_one_workflow_and_one_runs_row(
    temporal_address: str, task_queue: str, app_database: Database, session_id: str
) -> None:
    """Criterion 29 (P1): the server decides, not a check-then-start in our code."""
    run_id = f"run-{uuid.uuid4()}"

    async def race() -> set[str | None]:
        async with running_worker(temporal_address, task_queue, app_database) as client:
            handles = await asyncio.gather(
                *(
                    start_run(client, _start(run_id, session_id), task_queue=task_queue)
                    for _ in range(5)
                )
            )
            await asyncio.gather(*(progress_until(h, lambda p: p.recorded) for h in handles))
            return {h.first_execution_run_id for h in handles}

    assert len(asyncio.run(race())) == 1
    row = app_database.fetch_one("SELECT count(*) FROM runs WHERE run_id = %s", (run_id,))
    assert row == (1,)


def test_the_worker_the_cli_serves_runs_what_it_is_handed(
    temporal_address: str,
    task_queue: str,
    app_database_url: str,
    app_database: Database,
    session_id: str,
) -> None:
    """The entry point's own wiring, not a test-built worker: pool, stores, scorer,
    guard. Served in-process and cancelled, since a real `agentstack-worker` process
    would stop at preflight here without TabPFN's weights."""
    run_id = f"run-{uuid.uuid4()}"

    async def serve_one() -> RunProgress:
        serving = asyncio.create_task(
            worker_cli._serve(temporal_address, task_queue, app_database_url)
        )
        try:
            client = await connect_temporal(temporal_address)
            handle = await start_run(client, _start(run_id, session_id), task_queue=task_queue)
            return await progress_until(handle, lambda p: p.recorded)
        finally:
            serving.cancel()
            with suppress(asyncio.CancelledError):
                await serving

    assert asyncio.run(serve_one()).recorded
    assert RunStore(db=app_database).get(run_id) is not None


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


# --- rule 2: a refusal is an answer, and asking again repeats it (criterion 35) ---

# The real classes, imported from where they're raised. The policy holds their names,
# because workflow code may not import them.
REFUSAL_TYPES: dict[str, type[Exception]] = {
    cls.__name__: cls
    for cls in (
        UnresolvedEffect,
        SurfaceRefused,
        SandboxViolation,
        PolicyDenied,
        ApprovalRequired,
        ApprovalStale,
        ApproverNotAuthorized,
        OutcomeNotAuthorized,
    )
}


# Everything the policy stops after one attempt, as the exception an activity would raise.
FAILS_ONCE: dict[str, Callable[[str], Exception]] = {
    **REFUSAL_TYPES,
    IntegrityViolation.__name__: lambda message: IntegrityViolation(
        "runs_session_id_fkey", message
    ),
}


def test_the_policy_names_every_refusal_and_only_real_ones() -> None:
    assert set(REFUSALS) == set(REFUSAL_TYPES)
    assert set(DETERMINISTIC) == {IntegrityViolation.__name__}
    assert set(RETRY.non_retryable_error_types or ()) == {*FAILS_ONCE, UNDECLARED}


def test_every_activity_the_workflow_runs_uses_the_one_policy() -> None:
    tree = ast.parse((TEMPORAL / "workflows.py").read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute_activity", "start_activity"}
    ]
    assert calls, "the workflow runs no activities"
    for call in calls:
        policies = [k.value for k in call.keywords if k.arg == "retry_policy"]
        assert len(policies) == 1, ast.unparse(call)
        assert isinstance(policies[0], ast.Name) and policies[0].id == "RETRY", ast.unparse(call)


def test_each_refusal_and_each_deterministic_failure_is_attempted_exactly_once(
    temporal_address: str, task_queue: str
) -> None:
    attempts: dict[str, int] = dict.fromkeys(FAILS_ONCE, 0)

    @activity.defn(name="refuse")
    def refuse(name: str) -> None:
        attempts[name] += 1
        raise FAILS_ONCE[name](f"probe: {name}")

    async def run_all() -> list[str]:
        client = await connect_temporal(temporal_address)
        with ThreadPoolExecutor(max_workers=len(FAILS_ONCE)) as executor:
            async with Worker(
                client,
                task_queue=task_queue,
                workflows=[CallOnce],
                activities=[refuse],
                activity_executor=executor,
            ):
                outcomes = asyncio.gather(
                    *(
                        client.execute_workflow(
                            CallOnce.run,
                            args=["refuse", name],
                            id=f"refusal-{name}-{uuid.uuid4()}",
                            task_queue=task_queue,
                        )
                        for name in FAILS_ONCE
                    )
                )
                return await asyncio.wait_for(outcomes, RESULT_TIMEOUT_S)

    outcomes = asyncio.run(run_all())

    assert [o.split(":")[0] for o in outcomes] == list(FAILS_ONCE)
    assert attempts == dict.fromkeys(FAILS_ONCE, 1)


# --- rule 5: registered is not declared (criterion 37) ---


def test_an_undeclared_activity_is_refused_before_it_runs(
    temporal_address: str, task_queue: str
) -> None:
    """E4: registered beside the real activities, it would reach a surface directly."""
    reached: list[str] = []

    @activity.defn(name="rogue_rollout")
    def rogue_rollout(experiment_id: str) -> None:
        reached.append(experiment_id)

    async def run_it() -> str:
        client = await connect_temporal(temporal_address)
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with Worker(
                client,
                task_queue=task_queue,
                workflows=[CallOnce],
                activities=[rogue_rollout],
                activity_executor=executor,
                interceptors=[DeclaredActivitiesOnly()],
            ):
                result = client.execute_workflow(
                    CallOnce.run,
                    args=["rogue_rollout", "exp-1"],
                    id=f"rogue-{uuid.uuid4()}",
                    task_queue=task_queue,
                )
                return await asyncio.wait_for(result, RESULT_TIMEOUT_S)

    assert asyncio.run(run_it()) == f"{UNDECLARED}:non_retryable=True"
    assert reached == []


def test_the_production_worker_installs_the_guard_and_declares_all_it_registers(
    temporal_address: str,
) -> None:
    async def configured() -> dict[str, object]:
        client = await connect_temporal(temporal_address)
        with ThreadPoolExecutor(max_workers=1) as executor:
            db = Database.__new__(Database)  # never queried: this only reads the config
            activities = RunActivities(
                runs=RunStore(db=db), cycles=CycleStore(db=db), scorer=NoScoring()
            )
            worker = build_worker(client, activities=activities, executor=executor)
            return dict(worker.config())

    config = asyncio.run(configured())

    interceptors = config["interceptors"]
    assert isinstance(interceptors, list)
    assert any(isinstance(i, DeclaredActivitiesOnly) for i in interceptors)
    registered = config["activities"]
    assert isinstance(registered, list)
    # Where `@activity.defn` records the name a worker registers the function under.
    names = {getattr(fn, "__temporal_activity_definition").name for fn in registered}
    assert names <= set(DECLARED), f"registered but undeclared: {names - set(DECLARED)}"
