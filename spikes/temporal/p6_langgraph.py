"""P6: the LangGraph plugin - nodes as activities, interrupt resumed by an Update (cf. ADR-0006)."""

import asyncio
import uuid
from datetime import timedelta

from _env import local_env
from p6_defs import Turn, g
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.worker import Worker
from workflows import ledger_lines


async def main():
    plugin = LangGraphPlugin(
        graphs={"turn": g},
        default_activity_options={"start_to_close_timeout": timedelta(seconds=10)},
    )
    async with (
        await local_env() as env,
        Worker(env.client, task_queue="p6", workflows=[Turn], plugins=[plugin]),
    ):
        tag = str(uuid.uuid4())
        h = await env.client.start_workflow(Turn.run, tag, id=tag, task_queue="p6")
        while "before-interrupt" not in [r["what"] for r in ledger_lines(tag)]:
            await asyncio.sleep(0.1)
        await h.signal(Turn.respond, "yes")
        assert await h.result() == "yes"
        whats = [r["what"] for r in ledger_lines(tag)]
        acts = [
            e.activity_task_scheduled_event_attributes.activity_type.name
            for e in (await h.fetch_history()).events
            if e.HasField("activity_task_scheduled_event_attributes")
        ]
        print(f"  effects: {whats}\n  activities scheduled: {acts}")
        assert whats.count("draft") == 1 and whats[-1] == "commit:yes"
        print(
            f"P6 PASS: graph runs as activities, no Postgres checkpointer; "
            f"code before interrupt() ran {whats.count('before-interrupt')}x"
        )


asyncio.run(main())
