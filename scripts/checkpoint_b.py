"""Checkpoint B: one real trigger through the real stack, and what the draft turn did.

    agentstack-worker --scores recorded --profile default --task-queue hackathon-checkpoint-b
    PYTHONPATH=src python scripts/checkpoint_b.py

Sends a KKBox data-arrival trigger to a running worker (Temporal + Postgres from
`scripts/dev_up.sh`, Ollama qwen3:8b, TabPFN scores replayed from `data/scores/`), waits for
the drafting turn, then prints from the record: the cohort TabPFN's scores froze, the
instruction the model was given, and the draft it wrote. Nothing is read from the model's
memory of the run; every line comes from Postgres.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

import agentstack
from agentstack.context.datasets import load
from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.interfaces.wiring import build_stack, deliver
from agentstack.runtime.temporal.client import connect, temporal_address
from agentstack.runtime.temporal.contracts import workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import database_url, open_pool
from agentstack.tools.spec import Surface

TENANT = "acme"
TIMEOUT_S = 900.0  # one draft turn on qwen3:8b, with a retry or two


async def main(task_queue: str, dataset: str) -> None:
    print(f"code     : {agentstack.__file__}")
    snapshot = load(dataset)
    experiment_id = f"exp-{uuid.uuid4().hex[:6]}"
    payload = {
        "kind": "data_arrival",
        "experiment_id": experiment_id,
        "data_as_of": snapshot.data_as_of,
        "tenant": TENANT,
    }
    print(f"trigger  : {payload}")
    client = await connect(temporal_address())
    url = database_url()
    checkpoints, saver = open_checkpointer(url)
    with open_pool(url, min_size=1, max_size=2) as pool:
        db = Database(pool=pool)
        stack = build_stack(db, saver)
        run_id = await deliver(stack, client, payload, source="checkpoint-b", task_queue=task_queue)
        handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
        started = time.monotonic()
        while True:
            progress = await handle.query(ExperimentWorkflow.progress)
            if progress.turns or time.monotonic() - started > TIMEOUT_S:
                break
            await asyncio.sleep(1)
        print(
            f"waited   : {time.monotonic() - started:.0f}s; cycles={len(progress.cycles)} "
            f"turns={len(progress.turns)}"
        )
        if not progress.cycles or progress.cycles[0].outcome != "propose":
            print(f"no proposal: {progress.cycles}")
            return
        version = progress.cycles[0].experiment_version
        frozen = FrozenCohortStore(db=db).get(
            tenant=TENANT, experiment_id=experiment_id, experiment_version=version
        )
        assert frozen is not None
        print("\n== the cohort TabPFN's scores froze ==")
        print(json.dumps(frozen.description, indent=2))

        run = stack.runs.get(run_id)
        assert run is not None
        events = stack.transcripts.for_session(run.session_id)
        print("\n== what the drafting turn was told ==")
        print(next(e.body for e in events if e.kind == "user"))
        print("\n== what it replied ==")
        print(next((e.body for e in events if e.kind == "agent"), "(no reply recorded)"))

        draft = stack.deps.gateway.surfaces[Surface.REGISTRY].read(
            f"{TENANT}/experiments/{experiment_id}", {}
        )
        print("\n== the draft in the registry ==")
        print(json.dumps(draft, indent=2))
        print(f"\nturn outcome: {progress.turns[0]}")
    checkpoints.close() if hasattr(checkpoints, "close") else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-queue", default="hackathon-checkpoint-b")
    parser.add_argument("--dataset", default="kkbox-churn")
    args = parser.parse_args()
    asyncio.run(main(args.task_queue, args.dataset))
