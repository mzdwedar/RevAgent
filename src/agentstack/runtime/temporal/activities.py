"""The workflow's hands: each activity is a thin shell over runtime code that already exists.

Activities are at-least-once (P2). A completion can be lost after the work landed, and
the activity then runs again. So every write here is an upsert or a claim, and a rerun
converges on the same record. If an activity grows a decision of its own, that decision
is in the wrong layer.

Arguments and results are ids from `contracts`. Anything else the activity needs, it
reads from Postgres itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from temporalio import activity

from agentstack.context.targeting import TargetingRule
from agentstack.policy.triggers import TriggerEvent, TriggerKind
from agentstack.prediction.churn import ChurnScorer
from agentstack.runtime import cycles
from agentstack.runtime.operator import evaluate_trigger
from agentstack.runtime.run import Run, RunStore
from agentstack.runtime.temporal.contracts import (
    ENSURE_RUN,
    EVALUATE_CYCLE,
    PARK_TRIGGER_WAIT,
    SATISFY_TRIGGER_WAIT,
    CycleResult,
    ParkedWait,
    RunStart,
    Trigger,
    TriggerArrived,
    TriggerWaitIntent,
)
from agentstack.runtime.waits import TRIGGER, ResumeEvent, WaitStore, resume


class RunActivities:
    """Activities bound to the stores they write, built once per worker."""

    def __init__(
        self,
        *,
        runs: RunStore,
        cycles: cycles.CycleStore,
        waits: WaitStore,
        scorer: ChurnScorer,
        trigger_deadline: timedelta,
        rule: TargetingRule | None = None,
    ) -> None:
        self._runs = runs
        self._cycles = cycles
        self._waits = waits
        self._scorer = scorer
        self._trigger_deadline = trigger_deadline
        self._rule = rule

    @activity.defn(name=ENSURE_RUN)
    def ensure_run(self, start: RunStart) -> str:
        run = Run(
            run_id=start.run_id,
            session_id=start.session_id,
            tenant=start.tenant,
            user=start.user,
            stage=start.stage,
            channel=start.channel,
        )
        return self._runs.ensure(run).run_id

    @activity.defn(name=EVALUATE_CYCLE)
    def evaluate_cycle(self, trigger: Trigger) -> CycleResult:
        """`cycles.evaluate` with the operator's evaluator in its seam.

        A rerun after the claim landed returns the cycle that's already there without
        scoring again, which is what makes this safe to run at least once.
        """
        event = TriggerEvent(
            kind=TriggerKind(trigger.kind),
            experiment_id=trigger.experiment_id,
            data_as_of=trigger.data_as_of,
            tenant=trigger.tenant,
            source=trigger.source,
        )
        cycle = cycles.evaluate(
            self._cycles,
            event,
            lambda e: evaluate_trigger(e, scorer=self._scorer, rule=self._rule).as_seam_result(),
        )
        return CycleResult(
            experiment_id=cycle.experiment_id,
            data_as_of=cycle.data_as_of,
            kind=cycle.kind.value,
            outcome=None if cycle.outcome is None else cycle.outcome.value,
            # `trigger_cycles.run_id` holds the experiment version the cycle froze.
            experiment_version=cycle.run_id,
        )

    @activity.defn(name=PARK_TRIGGER_WAIT)
    def park_trigger_wait(self, intent: TriggerWaitIntent) -> ParkedWait:
        """Write the `waits` row that makes a missing trigger visible (criterion 4).

        The deadline is the record's: `operator stalled` reads it from this row, and a
        rerun gets the deadline the first attempt wrote, not a later one.
        """
        wait = self._waits.park(
            wait_id=f"wait-{intent.run_id}-trigger-{intent.sequence}",
            run_id=intent.run_id,
            kind=TRIGGER,
            state_snapshot=intent.after_data_as_of,
            timeout=self._trigger_deadline,
        )
        assert wait.deadline is not None  # park refuses a trigger wait without one
        due_in = wait.deadline - datetime.now(UTC)
        return ParkedWait(wait_id=wait.wait_id, due_in_s=due_in.total_seconds())

    @activity.defn(name=SATISFY_TRIGGER_WAIT)
    def satisfy_trigger_wait(self, arrived: TriggerArrived) -> str:
        """The trigger came: satisfy its wait, once, however many times this runs.

        A rerun after the first attempt landed finds the wait satisfied and stops there.
        Two attempts overlapping is settled by `resume`'s conditional update: the loser
        is refused, retried, and then finds it satisfied.
        """
        wait = self._waits.get(arrived.wait_id)
        assert wait is not None  # parked by this run before it waited
        if not wait.satisfied:
            resume(
                self._waits,
                ResumeEvent(
                    run_id=wait.run_id,
                    wait_id=wait.wait_id,
                    state_snapshot=wait.state_snapshot,
                    payload={
                        "kind": arrived.trigger.kind,
                        "experiment_id": arrived.trigger.experiment_id,
                        "data_as_of": arrived.trigger.data_as_of,
                    },
                ),
            )
        return arrived.wait_id
