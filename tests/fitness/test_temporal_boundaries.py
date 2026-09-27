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
import uuid
from pathlib import Path

from temporalio.worker import Worker

from agentstack.runtime.temporal.contracts import RunEnd, RunStart, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from tests.conftest import connect_temporal

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


def test_the_experiment_workflow_runs_on_the_dev_server(
    temporal_address: str, task_queue: str
) -> None:
    async def run_once() -> RunEnd:
        client = await connect_temporal(temporal_address)
        async with Worker(client, task_queue=task_queue, workflows=[ExperimentWorkflow]):
            run_id = f"run-{uuid.uuid4()}"
            return await client.execute_workflow(
                ExperimentWorkflow.run,
                RunStart(run_id=run_id),
                id=workflow_id(run_id),
                task_queue=task_queue,
            )

    end = asyncio.run(run_once())
    assert end.status == "complete"
