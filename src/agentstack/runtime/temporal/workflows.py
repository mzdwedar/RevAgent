"""The experiment run as a Temporal workflow.

Workflow code is replayed from history on every recovery, so it must be deterministic:
no I/O, no clock, no randomness except through `workflow.*`. The sandbox catches some
of that. It does not catch an import of `execution` or `storage` (E4), which is why
contract 6 forbids them here and activities are named through `contracts`, never
imported.
"""

from __future__ import annotations

from temporalio import workflow

from agentstack.runtime.temporal.contracts import RunEnd, RunStart


@workflow.defn
class ExperimentWorkflow:
    """Owns where the run is. Owns nothing else: no I/O, no clock, no authority."""

    @workflow.run
    async def run(self, start: RunStart) -> RunEnd:
        return RunEnd(run_id=start.run_id, status="complete")
