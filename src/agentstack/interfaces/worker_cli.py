"""The Temporal worker: where runs actually execute.

    uv run agentstack-worker

Refuses to poll until it could finish what it picks up. Preflight comes first
(criterion 18: a run must not park on a human and then discover it cannot score), then
the server. Either one missing is a non-zero exit at start, never a hang (criterion 45).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable

from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.interfaces import preflight_cli
from agentstack.interfaces.slack import SlackNotifier
from agentstack.interfaces.wiring import ChannelAsker, ExperimentTurns, build_stack
from agentstack.model.ollama_engine import OllamaEngine
from agentstack.prediction.engine import TabPFNScorer
from agentstack.runtime.cadence import TriggerCadence
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.run import RunStore
from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.client import TemporalUnavailable, connect, temporal_address
from agentstack.runtime.temporal.contracts import TASK_QUEUE
from agentstack.runtime.temporal.worker import (
    MAX_CONCURRENT_ACTIVITIES,
    activity_threads,
    build_worker,
)
from agentstack.runtime.waits import WaitStore
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import database_url, open_pool, redacted


def main(
    argv: list[str] | None = None,
    *,
    preflight: Callable[[list[str]], int] = preflight_cli.main,
) -> int:
    parser = argparse.ArgumentParser(description="Run experiment workflows and their activities.")
    parser.add_argument("--address", default=None, help="Temporal; defaults to $TEMPORAL_ADDRESS")
    parser.add_argument("--task-queue", default=TASK_QUEUE)
    parser.add_argument("--url", default=None, help="database URL; defaults to $DATABASE_URL")
    args = parser.parse_args(argv)

    if preflight([]) != 0:
        print("FAIL  preflight refused; not polling for work", file=sys.stderr)
        return 1
    try:
        asyncio.run(_serve(args.address or temporal_address(), args.task_queue, args.url))
    except TemporalUnavailable as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    return 0


async def _serve(address: str, task_queue: str, url: str | None) -> None:
    client = await connect(address)
    url = url or database_url()
    print(f"temporal : {address}  queue {task_queue}")
    print(f"database : {redacted(url)}")
    checkpoints, saver = open_checkpointer(url)
    try:
        with (
            # Activities hold one pooled connection each, so the pool is sized to the
            # worker's activity bound.
            open_pool(url, min_size=1, max_size=MAX_CONCURRENT_ACTIVITIES) as pool,
            activity_threads() as executor,
        ):
            db = Database(pool=pool)
            stack = build_stack(db, saver)
            # The model engine a turn drafts with (SPEC.md: qwen3:8b through Ollama), and
            # the channel approvals are asked in (needs SLACK_BOT_TOKEN; a missing token
            # fails each ask loudly, and the wait stays pending and visible).
            stack.deps.engine = OllamaEngine()
            stack.notifier = SlackNotifier()
            activities = RunActivities(
                runs=RunStore(db=db),
                cycles=CycleStore(db=db),
                cohorts=FrozenCohortStore(db=db),
                waits=WaitStore(db=db),
                # The scorer preflight just proved can load its weights.
                scorer=TabPFNScorer(),
                trigger_deadline=TriggerCadence.load().trigger_deadline,
                turns=ExperimentTurns(stack),
                asker=ChannelAsker(stack.notifier),
            )
            worker = build_worker(
                client, activities=activities, executor=executor, task_queue=task_queue
            )
            await worker.run()
    finally:
        checkpoints.close()


if __name__ == "__main__":
    sys.exit(main())
