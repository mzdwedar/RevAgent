"""P5: the Replayer catches a deploy that would break live runs (cf. T19/T20 checkpoint_guard)."""

from datetime import timedelta

from temporalio import workflow
from workflows import record

OPTS = dict(start_to_close_timeout=timedelta(seconds=10))


@workflow.defn(name="Versioned")
class V1:
    @workflow.run
    async def run(self, tag: str) -> None:
        await workflow.execute_activity(record, args=[tag, "score"], **OPTS)
        await workflow.execute_activity(record, args=[tag, "draft"], **OPTS)


@workflow.defn(name="Versioned")
class V2Unsafe:  # new step inserted, no guard
    @workflow.run
    async def run(self, tag: str) -> None:
        await workflow.execute_activity(record, args=[tag, "score"], **OPTS)
        await workflow.execute_activity(record, args=[tag, "target"], **OPTS)
        await workflow.execute_activity(record, args=[tag, "draft"], **OPTS)


@workflow.defn(name="Versioned")
class V2Patched:
    @workflow.run
    async def run(self, tag: str) -> None:
        await workflow.execute_activity(record, args=[tag, "score"], **OPTS)
        if workflow.patched("add-targeting"):
            await workflow.execute_activity(record, args=[tag, "target"], **OPTS)
        await workflow.execute_activity(record, args=[tag, "draft"], **OPTS)
