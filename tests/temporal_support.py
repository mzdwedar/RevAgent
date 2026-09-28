"""A real worker in the test's own process, and a bounded way to watch a run.

The run is open-ended (one per experiment, living for weeks), so a test can't await
its result. It reads the `progress` query until what it expects is there, and fails
when that doesn't happen in time. A failing activity is retried until its policy
says stop, so every wait here is bounded: a failure fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio.client import Client, WorkflowHandle, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.context.targeting import TargetingRule
from agentstack.interfaces.wiring import ChannelAsker, ExperimentTurns, Stack
from agentstack.model.contract import ModelAsset, ModelRequest, ModelResponse, ToolCallProposal
from agentstack.prediction.churn import ChurnScores
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.run import RunStore
from agentstack.runtime.temporal.activities import Asker, RunActivities, TurnHost
from agentstack.runtime.temporal.contracts import RunProgress
from agentstack.runtime.temporal.worker import build_worker
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.runtime.waits import WaitStore
from agentstack.storage.database import Database
from tests.conftest import connect_temporal

WAIT_S = 30.0
# What `scripts/replay_guard.py` replays, and the switch that lets a test write there.
HISTORIES = Path(__file__).resolve().parent / "fixtures" / "histories"
RECORD_HISTORIES = "AGENTSTACK_RECORD_HISTORIES"
# Long enough that no test on the real server ever sees a trigger wait go overdue by
# accident. The tests that are about deadlines set their own, under time-skipping.
DEFAULT_TRIGGER_DEADLINE = timedelta(days=1)


DRAFT_TOOL = "create_experiment_draft"
ROLLOUT_TOOL = "roll_out_variant_to_percentage"


def model_calls(path: Path, *, tool: str) -> list[str]:
    """The pids that asked the model with `tool` shown, from a DraftingEngine calls file."""
    lines = path.read_text().split("\n") if path.exists() else []
    return [pid for pid, shown in (line.split() for line in lines if line) if shown == tool]


class DraftingEngine:
    """Stands in for the model on a turn, and does what the instruction asks.

    On a drafting turn it takes tenant, experiment and version from the rendered
    context, exactly as told, and writes its own hypothesis and variant. On a rollout
    turn it proposes the rollout it was told, argument for argument. A model shown
    neither tool proposes nothing. Every call is recorded to `calls`, a file when the
    count must survive a killed process.
    """

    asset = ModelAsset(name="drafting-stub", context_window=8192, max_output_tokens=512)

    def __init__(self, calls: Path | None = None) -> None:
        self.calls_file = calls
        self.calls = 0
        # One entry per call: the tool this call was shown to act with ("none" if neither).
        self.shown: list[str] = []

    @property
    def drafts(self) -> int:
        """How many times the model was asked to draft: what a drafting turn must not repeat."""
        return self.shown.count(DRAFT_TOOL)

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        tool = next((t for t in (ROLLOUT_TOOL, DRAFT_TOOL) if t in request.tool_names), "none")
        self.shown.append(tool)
        if self.calls_file is not None:
            with self.calls_file.open("a") as handle:
                handle.write(f"{os.getpid()} {tool}\n")
        return self._answer(request)

    def _answer(self, request: ModelRequest) -> ModelResponse:
        told = dict(
            token.split("=", 1) for token in request.rendered_context.split() if "=" in token
        )
        if "roll_out_variant_to_percentage" in request.tool_names:
            rollout: dict[str, Any] = {
                key: told[key]
                for key in (
                    "tenant",
                    "experiment_id",
                    "experiment_version",
                    "targeting_model_version",
                )
            }
            rollout |= {
                "percentage": int(told["percentage"]),
                "risk_threshold": float(told["risk_threshold"]),
            }
            return ModelResponse(
                text="proposed the rollout",
                proposals=(
                    ToolCallProposal(tool="roll_out_variant_to_percentage", arguments=rollout),
                ),
            )
        if "create_experiment_draft" not in request.tool_names:
            return ModelResponse(text="nothing I was shown can draft this")
        arguments = {key: told[key] for key in ("tenant", "experiment_id", "experiment_version")}
        arguments |= {
            "hypothesis": "a discount retains the customers most at risk",
            "variant": "20-percent-off",
        }
        return ModelResponse(
            text="drafted the experiment",
            proposals=(ToolCallProposal(tool="create_experiment_draft", arguments=arguments),),
        )


class NoScoring:
    """For runs that are never handed a trigger. Scoring one would be a test bug."""

    model_version = "none"

    def score(self, **_: Any) -> ChurnScores:
        raise AssertionError("this test's run was not meant to score anything")


def activities_for(
    db: Database,
    *,
    scorer: Any = None,
    rule: TargetingRule | None = None,
    trigger_deadline: timedelta = DEFAULT_TRIGGER_DEADLINE,
    turns: TurnHost | None = None,
    asker: Asker | None = None,
) -> RunActivities:
    return RunActivities(
        runs=RunStore(db=db),
        cycles=CycleStore(db=db),
        cohorts=FrozenCohortStore(db=db),
        waits=WaitStore(db=db),
        scorer=scorer or NoScoring(),
        trigger_deadline=trigger_deadline,
        rule=rule,
        turns=turns,
        asker=asker,
    )


def turns_for(stack: Stack, engine: Any) -> ExperimentTurns:
    """The production turn host, over a test stack, with a test engine."""
    stack.deps.engine = engine
    return ExperimentTurns(stack)


def asker_for(stack: Stack) -> ChannelAsker:
    """The production asker, posting to the test stack's recording notifier."""
    return ChannelAsker(stack.notifier)


@asynccontextmanager
async def worker_on(
    client: Client, task_queue: str, db: Database, **options: Any
) -> AsyncIterator[Client]:
    """The production worker, with test stores and scorer, polling `task_queue`."""
    with ThreadPoolExecutor(max_workers=4) as executor:
        worker = build_worker(
            client,
            activities=activities_for(db, **options),
            executor=executor,
            task_queue=task_queue,
        )
        async with worker:
            yield client


@asynccontextmanager
async def running_worker(
    address: str, task_queue: str, db: Database, **options: Any
) -> AsyncIterator[Client]:
    client = await connect_temporal(address)
    async with worker_on(client, task_queue, db, **options):
        yield client


@asynccontextmanager
async def time_skipping() -> AsyncIterator[WorkflowEnvironment]:
    """Temporal's time-skipping test server: days of timers in a second (P8).

    The binary is fetched on first use, at the version the SDK pins, so `uv.lock` pins
    it too. CI caches it in `TEMPORAL_TEST_SERVER_DIR`. If it can't be had, the test
    fails and says why. It never skips.
    """
    try:
        env = await WorkflowEnvironment.start_time_skipping(
            download_dest_dir=os.environ.get("TEMPORAL_TEST_SERVER_DIR")
        )
    except Exception as exc:
        raise AssertionError(
            f"the time-skipping test server could not start ({type(exc).__name__}: {exc}). "
            "It is downloaded on first use; set TEMPORAL_TEST_SERVER_DIR to a directory "
            "holding it to run offline."
        ) from exc
    async with env:
        yield env


async def progress_until(
    handle: WorkflowHandle[ExperimentWorkflow, Any],
    done: Callable[[RunProgress], bool],
    *,
    timeout: float = WAIT_S,
) -> RunProgress:
    """Poll the run's position until `done` says so, or fail with where it got to."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        progress = await handle.query(ExperimentWorkflow.progress)
        if done(progress):
            return progress
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"run did not get there in {timeout}s; it is at {progress}")
        await asyncio.sleep(0.1)


async def keep_history(handle: WorkflowHandle[ExperimentWorkflow, Any], name: str) -> Path | None:
    """The run's history so far, as the file it would be: replayed against this code,
    and written to `HISTORIES/{name}.json` only if asked.

    Replayed every time, so a scenario that no longer replays is found by the suite and
    not by the next person to record. Written only when `RECORD_HISTORIES` is set
    (`replay_guard.py --record` sets it): a fixture changes because someone meant it
    to, never as a side effect of running the tests.

    A failed activity's stack trace names the disk it ran on: the checkout, the
    interpreter, the user. Replay never reads one, so it is emptied. The failure's type
    and message, which the workflow does read, are kept.
    """
    history = await handle.fetch_history()
    recorded = _without_stack_traces(history.to_json_dict())
    text = json.dumps(recorded, indent=1, sort_keys=True) + "\n"
    kept = WorkflowHistory.from_json(name, text)
    await Replayer(workflows=[ExperimentWorkflow]).replay_workflow(kept)
    if not os.environ.get(RECORD_HISTORIES):
        return None
    HISTORIES.mkdir(parents=True, exist_ok=True)
    path = HISTORIES / f"{name}.json"
    path.write_text(text)
    return path


def _without_stack_traces(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: "" if k == "stackTrace" else _without_stack_traces(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_without_stack_traces(v) for v in node]
    return node
