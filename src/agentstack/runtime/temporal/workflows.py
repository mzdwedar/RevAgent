"""The experiment run as a Temporal workflow.

Workflow code is replayed from history on every recovery, so it must be deterministic:
no I/O, no clock, no randomness except through `workflow.*`. The sandbox catches some
of that. It does not catch an import of `execution` or `storage` (E4), which is why
contract 6 forbids them here and activities are named through `contracts`, never
imported.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from agentstack.runtime.temporal.contracts import (
    ENSURE_RUN,
    EVALUATE_CYCLE,
    CycleResult,
    RunEnd,
    RunProgress,
    RunStart,
    Trigger,
)
from agentstack.runtime.temporal.retry import RETRY

RECORD_TIMEOUT = timedelta(seconds=30)
# Scoring is the expensive part of a cycle; SPEC.md budgets p95 evaluation at 60s.
CYCLE_TIMEOUT = timedelta(seconds=120)


@workflow.defn
class ExperimentWorkflow:
    """Owns where the run is. Owns nothing else: no I/O, no clock, no authority.

    One run per experiment, living for weeks (SPEC.md). It waits for triggers and runs
    one evaluation cycle per trigger it's handed, in the order they arrived.
    """

    def __init__(self) -> None:
        self._run_id = ""
        self._recorded = False
        self._pending: list[Trigger] = []
        self._cycles: list[CycleResult] = []

    @workflow.run
    async def run(self, start: RunStart) -> RunEnd:
        self._run_id = start.run_id
        # The record first. Temporal knows the run exists; Postgres has to know too,
        # because every wait, step and approval hangs off the `runs` row.
        await workflow.execute_activity(
            ENSURE_RUN, start, start_to_close_timeout=RECORD_TIMEOUT, retry_policy=RETRY
        )
        self._recorded = True
        while True:
            await workflow.wait_condition(lambda: bool(self._pending))
            self._cycles.append(await self._evaluate(self._pending.pop(0)))

    async def _evaluate(self, trigger: Trigger) -> CycleResult:
        """One cycle. A redelivered trigger is deduplicated by the Postgres claim inside
        the activity, not here: history is not the dedupe window (spec, criterion 30)."""
        try:
            result: CycleResult = await workflow.execute_activity(
                EVALUATE_CYCLE,
                trigger,
                result_type=CycleResult,
                start_to_close_timeout=CYCLE_TIMEOUT,
                retry_policy=RETRY,
            )
            return result
        except ActivityError as exc:
            # Only a non-retryable failure gets here. It's this cycle's answer, and the
            # run goes on to the next trigger: one refused cycle doesn't end an experiment.
            cause = exc.cause
            refusal = cause.type if isinstance(cause, ApplicationError) else None
            return CycleResult(
                experiment_id=trigger.experiment_id,
                data_as_of=trigger.data_as_of,
                kind=trigger.kind,
                outcome=None,
                refusal=refusal or type(cause).__name__,
            )

    @workflow.signal
    def trigger(self, trigger: Trigger) -> None:
        """A wake-up carrying ids. It grants nothing: the kind's authority is checked
        in the activity, against the outcome, by layer 8's table."""
        self._pending.append(trigger)

    @workflow.query
    def progress(self) -> RunProgress:
        return RunProgress(
            run_id=self._run_id,
            recorded=self._recorded,
            pending_triggers=len(self._pending),
            cycles=tuple(self._cycles),
        )
