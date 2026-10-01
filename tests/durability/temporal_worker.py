"""A real Temporal worker process, to be SIGKILLed from outside.

The production worker (`build_worker`) with the test's substrate: the fixture dataset
and a scorer that records each call to a file, since the whole question is what
survives between two processes. Not `agentstack-worker` itself, which stops at
preflight without TabPFN's weights. Its own wiring is covered in-process by
`test_temporal_boundaries`.

    python -m tests.durability.temporal_worker --address ... --task-queue ... \
        --database-url ... --scorer-calls FILE --ready FILE
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from agentstack.context import datasets
from agentstack.context.datasets import REGISTRY, CohortSnapshot, DatasetSpec
from agentstack.context.frozen_cohorts import FrozenCohort, FrozenCohortStore
from agentstack.interfaces.wiring import build_stack
from agentstack.observability.spans import LoggingSink
from agentstack.prediction.churn import ChurnScores
from agentstack.runtime.cadence import TriggerCadence
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.run import Run, RunStore
from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.client import connect
from agentstack.runtime.temporal.worker import (
    MAX_CONCURRENT_ACTIVITIES,
    activity_threads,
    build_worker,
)
from agentstack.runtime.waits import Wait, WaitStore
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import open_pool
from agentstack.tools.action import ActionRequest
from tests.fitness.test_trigger_to_candidate import RULE, StubScorer, snapshot
from tests.temporal_support import DraftingEngine, asker_for, turns_for

# A stray worker must not outlive the test that started it.
LIFETIME_S = 120.0


class RecordingScorer:
    """Appends a line per scoring call to a file both processes can read."""

    model_version = StubScorer.model_version

    def __init__(self, path: Path) -> None:
        self.path = path
        self.inner = StubScorer()

    def score(self, **kwargs: Any) -> ChurnScores:
        with self.path.open("a") as handle:
            handle.write(f"{os.getpid()}\n")
        return self.inner.score(**kwargs)


def _fixture_dataset() -> None:
    """What `tests/durability/conftest.py:fixture_dataset` does, for a process pytest
    doesn't run."""
    REGISTRY["fixture"] = DatasetSpec(
        key="fixture",
        kaggle="nobody/nothing",
        files=("fixture.csv",),
        target="Churn",
        churned="1",
        drops={},
        revenue_columns=("monthly charge",),
        revenue_periods_per_year=12,
        revenue_note="fixture revenue, annualised x12",
    )

    def only_the_fixture(key: str, root: Path | None = None) -> CohortSnapshot:
        assert key == "fixture" and root is None
        return snapshot()

    # Never undone: the process ends by being killed.
    pytest.MonkeyPatch().setattr(datasets, "load", only_the_fixture)


class HangingGateway:
    """Reads pass through; the first commit announces itself and then never returns.

    It hangs *before* the real gateway is called, so no idempotency claim exists yet:
    the kill lands after the model call was checkpointed and before any effect began.
    Killed inside the surface call instead, the retry would rightly meet an
    `UnresolvedEffect`, and that is T43's case, not this one.
    """

    def __init__(self, inner: Any, reached: Path) -> None:
        self.inner = inner
        self.reached = reached

    def read(self, **kwargs: Any) -> Any:
        return self.inner.read(**kwargs)

    def observe(self, **kwargs: Any) -> Any:
        return self.inner.observe(**kwargs)

    def execute(self, **kwargs: Any) -> Any:
        # Which tool got here, so the test knows the kill landed on the effect it meant.
        self.reached.write_text(kwargs["request"].tool)
        time.sleep(LIFETIME_S)
        raise AssertionError("a hanging gateway outlived its test")


class HangingAfterTheEffect(HangingGateway):
    """The first commit goes through the real gateway (the surface applies it, the ledger
    settles it) and then announces itself and never returns: the process dies between
    the effect and the step recording it, which is audit finding H1's window.
    """

    def execute(self, **kwargs: Any) -> Any:
        self.inner.execute(**kwargs)
        return super().execute(**kwargs)


class RecordingAsker:
    """The production asker, noting each question it puts in a file both processes can
    read: "asked once" has to be counted across a kill."""

    def __init__(self, inner: Any, path: Path) -> None:
        self.inner = inner
        self.path = path

    def ask(self, *, run: Run, wait: Wait, cohort: FrozenCohort, action: ActionRequest) -> str:
        message = str(self.inner.ask(run=run, wait=wait, cohort=cohort, action=action))
        with self.path.open("a") as handle:
            handle.write(f"{os.getpid()} {wait.wait_id}\n")
        return message


async def serve(args: argparse.Namespace) -> None:
    client = await connect(args.address)
    checkpoints, saver = open_checkpointer(args.database_url)
    try:
        with (
            open_pool(args.database_url, min_size=1, max_size=MAX_CONCURRENT_ACTIVITIES) as pool,
            activity_threads() as executor,
        ):
            db = Database(pool=pool)
            stack = build_stack(db, saver)
            model_calls = Path(args.model_calls or f"{args.ready}.model-calls")
            turns = turns_for(stack, DraftingEngine(calls=model_calls))
            if args.hang_before_gateway:
                stack.deps.gateway = HangingGateway(
                    stack.deps.gateway, Path(args.hang_before_gateway)
                )
            if args.hang_after_gateway:
                stack.deps.gateway = HangingAfterTheEffect(
                    stack.deps.gateway, Path(args.hang_after_gateway)
                )
            activities = RunActivities(
                runs=RunStore(db=db),
                cycles=CycleStore(db=db),
                cohorts=FrozenCohortStore(db=db),
                waits=WaitStore(db=db),
                scorer=RecordingScorer(Path(args.scorer_calls)),
                trigger_deadline=TriggerCadence.load().trigger_deadline,
                traces=LoggingSink(),
                versions=stack.deps.versions,
                audit=stack.audit,
                rule=RULE,
                turns=turns,
                asker=RecordingAsker(asker_for(stack), Path(args.asks or f"{args.ready}.asks")),
            )
            worker = build_worker(
                client, activities=activities, executor=executor, task_queue=args.task_queue
            )
            async with worker:
                Path(args.ready).write_text(str(os.getpid()))
                await asyncio.sleep(LIFETIME_S)
    finally:
        checkpoints.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--task-queue", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--scorer-calls", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--model-calls", default=None)
    parser.add_argument("--asks", default=None)
    parser.add_argument("--hang-before-gateway", default=None, metavar="REACHED_FILE")
    parser.add_argument("--hang-after-gateway", default=None, metavar="REACHED_FILE")
    args = parser.parse_args()
    _fixture_dataset()
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
