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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from agentstack.context import datasets
from agentstack.context.datasets import REGISTRY, CohortSnapshot, DatasetSpec
from agentstack.prediction.churn import ChurnScores
from agentstack.runtime.cadence import TriggerCadence
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.run import RunStore
from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.client import connect
from agentstack.runtime.temporal.worker import build_worker
from agentstack.runtime.waits import WaitStore
from agentstack.storage.database import Database
from agentstack.storage.pool import open_pool
from tests.fitness.test_trigger_to_candidate import RULE, StubScorer, snapshot

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


async def serve(args: argparse.Namespace) -> None:
    client = await connect(args.address)
    with (
        open_pool(args.database_url, min_size=1, max_size=4) as pool,
        ThreadPoolExecutor(max_workers=4) as executor,
    ):
        db = Database(pool=pool)
        activities = RunActivities(
            runs=RunStore(db=db),
            cycles=CycleStore(db=db),
            waits=WaitStore(db=db),
            scorer=RecordingScorer(Path(args.scorer_calls)),
            trigger_deadline=TriggerCadence.load().trigger_deadline,
            rule=RULE,
        )
        worker = build_worker(
            client, activities=activities, executor=executor, task_queue=args.task_queue
        )
        async with worker:
            Path(args.ready).write_text(str(os.getpid()))
            await asyncio.sleep(LIFETIME_S)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--task-queue", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--scorer-calls", required=True)
    parser.add_argument("--ready", required=True)
    args = parser.parse_args()
    _fixture_dataset()
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
