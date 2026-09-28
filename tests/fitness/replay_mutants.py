"""Deploys of `ExperimentWorkflow` for `test_replay_guard`: one that would strand live
runs, and the same change made safely.

Each registers under the real workflow's name, as a new deploy of the same code would,
and changes one thing: before a trigger is evaluated, it records the run again. A
step like that is what a real change looks like (a check added in front of an existing
one). The real workflow is never edited for this.

A module of their own because the workflow sandbox re-imports whatever defines a
workflow, and a test module drags pytest in with it.
"""

from __future__ import annotations

from temporalio import workflow

from agentstack.runtime.temporal.contracts import ENSURE_RUN, CycleResult, RunEnd, RunStart, Trigger
from agentstack.runtime.temporal.retry import RETRY
from agentstack.runtime.temporal.workflows import RECORD_TIMEOUT, ExperimentWorkflow

PATCH = "t47-record-before-evaluating"


@workflow.defn(name="ExperimentWorkflow")
class StepInsertedUnguarded(ExperimentWorkflow):
    """Every run from now on takes the new step, including those already under way."""

    @workflow.run
    async def run(self, start: RunStart) -> RunEnd:
        self._start = start
        return await super().run(start)

    async def _evaluate(self, trigger: Trigger) -> CycleResult:
        await workflow.execute_activity(
            ENSURE_RUN, self._start, start_to_close_timeout=RECORD_TIMEOUT, retry_policy=RETRY
        )
        return await super()._evaluate(trigger)


@workflow.defn(name="ExperimentWorkflow")
class StepInsertedBehindPatch(ExperimentWorkflow):
    """The same step, taken only by runs whose history says they took it, or that
    reach this point for the first time on the new code."""

    @workflow.run
    async def run(self, start: RunStart) -> RunEnd:
        self._start = start
        return await super().run(start)

    async def _evaluate(self, trigger: Trigger) -> CycleResult:
        if workflow.patched(PATCH):
            await workflow.execute_activity(
                ENSURE_RUN,
                self._start,
                start_to_close_timeout=RECORD_TIMEOUT,
                retry_policy=RETRY,
            )
        return await super()._evaluate(trigger)
