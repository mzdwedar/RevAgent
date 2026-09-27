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
    DRAFT,
    ENSURE_RUN,
    EVALUATE_CYCLE,
    PARK_TRIGGER_WAIT,
    RUN_TURN,
    SATISFY_TRIGGER_WAIT,
    CycleResult,
    ParkedWait,
    RunEnd,
    RunProgress,
    RunStart,
    Trigger,
    TriggerArrived,
    TriggerWaitIntent,
    TurnIntent,
    TurnOutcome,
)
from agentstack.runtime.temporal.retry import RETRY

RECORD_TIMEOUT = timedelta(seconds=30)
# Scoring is the expensive part of a cycle; SPEC.md budgets p95 evaluation at 60s.
CYCLE_TIMEOUT = timedelta(seconds=120)
# A turn's p95 is 60s too (SPEC.md). It heartbeats, so a dead worker is noticed in
# HEARTBEAT_TIMEOUT rather than after the whole two minutes.
TURN_TIMEOUT = timedelta(seconds=120)
HEARTBEAT_TIMEOUT = timedelta(seconds=15)


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
        # How many trigger waits this run has parked; names the next one. Replay
        # rebuilds it from history, so a rerun parks the same wait, not another.
        self._waits_parked = 0
        self._last_watermark = "none"
        self._waiting_on: str | None = None
        self._overdue = False
        self._turns: list[TurnOutcome] = []

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
            if not self._pending:
                await self._wait_for_trigger()
            trigger = self._pending.pop(0)
            cycle = await self._evaluate(trigger)
            self._cycles.append(cycle)
            self._last_watermark = trigger.data_as_of
            if cycle.outcome == "propose":
                # A frozen cohort is worth an experiment. Drafting it is the model's job,
                # in a turn; what the draft may commit is decided by policy (PRE_COMMIT).
                self._turns.append(await self._take_turn(DRAFT, cycle))

    async def _take_turn(self, stage: str, cycle: CycleResult) -> TurnOutcome:
        intent = TurnIntent(
            run_id=self._run_id,
            stage=stage,
            experiment_id=cycle.experiment_id,
            data_as_of=cycle.data_as_of,
            kind=cycle.kind,
        )
        try:
            outcome: TurnOutcome = await workflow.execute_activity(
                RUN_TURN,
                intent,
                result_type=TurnOutcome,
                start_to_close_timeout=TURN_TIMEOUT,
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                retry_policy=RETRY,
            )
            return outcome
        except ActivityError as exc:
            # A refusal (non-retryable) is this turn's answer; the run goes on.
            cause = exc.cause
            refusal = cause.type if isinstance(cause, ApplicationError) else None
            return TurnOutcome(status="refused", refusal=refusal or type(cause).__name__)

    async def _wait_for_trigger(self) -> None:
        """Waiting is state (Part 4). The `waits` row says what the run is waiting for
        and by when; this only decides when to look again.

        Past the deadline nothing expires. The run is overdue, `operator stalled` says
        so from the row, and it goes on waiting: a trigger that never fires has to
        surface, not end the run (SPEC.md criterion 4).
        """
        intent = TriggerWaitIntent(
            run_id=self._run_id,
            sequence=self._waits_parked,
            after_data_as_of=self._last_watermark,
        )
        parked: ParkedWait = await workflow.execute_activity(
            PARK_TRIGGER_WAIT,
            intent,
            result_type=ParkedWait,
            start_to_close_timeout=RECORD_TIMEOUT,
            retry_policy=RETRY,
        )
        self._waits_parked += 1
        self._waiting_on = parked.wait_id
        try:
            await workflow.wait_condition(
                lambda: bool(self._pending),
                timeout=timedelta(seconds=max(parked.due_in_s, 0.0)),
            )
        except TimeoutError:
            self._overdue = True
            await workflow.wait_condition(lambda: bool(self._pending))
        await workflow.execute_activity(
            SATISFY_TRIGGER_WAIT,
            TriggerArrived(wait_id=parked.wait_id, trigger=self._pending[0]),
            start_to_close_timeout=RECORD_TIMEOUT,
            retry_policy=RETRY,
        )
        self._waiting_on = None
        self._overdue = False

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
            waiting_on=self._waiting_on,
            overdue=self._overdue,
            turns=tuple(self._turns),
        )
