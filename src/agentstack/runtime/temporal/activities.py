"""The workflow's hands: each activity is a thin shell over runtime code that already exists.

Activities are at-least-once (P2). A completion can be lost after the work landed, and
the activity then runs again. So every write here is an upsert or a claim, and a rerun
converges on the same record. If an activity grows a decision of its own, that decision
is in the wrong layer.

Arguments and results are ids from `contracts`. Anything else the activity needs, it
reads from Postgres itself.
"""

from __future__ import annotations

import contextvars
import dataclasses
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.context.targeting import TargetingRule
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.policy.triggers import Outcome, TriggerEvent, TriggerKind
from agentstack.prediction.churn import ChurnScorer
from agentstack.runtime import cycles
from agentstack.runtime.drafting import draft_instruction
from agentstack.runtime.graph import finished_turn, turn_thread
from agentstack.runtime.loop import TurnDeps
from agentstack.runtime.loop import run_turn as take_turn
from agentstack.runtime.operator import Evaluation, evaluate_trigger
from agentstack.runtime.run import Run, RunStore
from agentstack.runtime.temporal.contracts import (
    ENSURE_RUN,
    EVALUATE_CYCLE,
    PARK_TRIGGER_WAIT,
    RUN_TURN,
    SATISFY_TRIGGER_WAIT,
    CycleResult,
    ParkedWait,
    RunStart,
    Trigger,
    TriggerArrived,
    TriggerWaitIntent,
    TurnIntent,
    TurnOutcome,
)
from agentstack.runtime.waits import TRIGGER, ResumeEvent, WaitStore, resume

# A turn's p95 is 60s (SPEC.md), and the workflow gives it 120s. A heartbeat every few
# seconds lets Temporal notice a dead worker in `HEARTBEAT_TIMEOUT` instead of waiting
# out the full two minutes.
HEARTBEAT_EVERY_S = 5.0

NO_TURN_HOST = "NoTurnHost"


class TurnHost(Protocol):
    """What a turn needs from above the runtime: the composition root supplies it.

    The runtime can't reach the control plane (contract 1), so session work comes in
    through here, exactly as it does for `wiring.handle`. Both calls happen inside the
    activity, at the turn: the envelope is minted for this act and never carried.
    """

    @property
    def deps(self) -> TurnDeps: ...

    def envelope(self, run: Run) -> IdentityEnvelope: ...

    def record(self, run: Run, *, asked: str, answered: str) -> None: ...


class RunActivities:
    """Activities bound to the stores they write, built once per worker."""

    def __init__(
        self,
        *,
        runs: RunStore,
        cycles: cycles.CycleStore,
        cohorts: FrozenCohortStore,
        waits: WaitStore,
        scorer: ChurnScorer,
        trigger_deadline: timedelta,
        rule: TargetingRule | None = None,
        turns: TurnHost | None = None,
    ) -> None:
        self._runs = runs
        self._cycles = cycles
        self._cohorts = cohorts
        self._waits = waits
        self._scorer = scorer
        self._trigger_deadline = trigger_deadline
        self._rule = rule
        self._turns = turns

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
        """`cycles.evaluate_to_settled` with the operator's evaluator in its seam.

        A rerun after the cycle settled returns it without scoring again. A rerun after
        an attempt that died before settling evaluates it (Checkpoint J). Either way it
        is safe to run at least once, and it never reports "no outcome" as an answer.
        """
        event = TriggerEvent(
            kind=TriggerKind(trigger.kind),
            experiment_id=trigger.experiment_id,
            data_as_of=trigger.data_as_of,
            tenant=trigger.tenant,
            source=trigger.source,
        )
        evaluated: list[Evaluation] = []

        def evaluator(e: TriggerEvent) -> tuple[Outcome, str | None]:
            evaluated.append(evaluate_trigger(e, scorer=self._scorer, rule=self._rule))
            return evaluated[-1].as_seam_result()

        def freeze(outcome: Outcome, _: str | None) -> None:
            # Authorised and not yet settled: the one moment the cohort is recorded (T40b).
            cohort = evaluated[-1].cohort if evaluated else None
            if outcome is Outcome.PROPOSE and cohort is not None:
                self._cohorts.record(
                    tenant=event.tenant, experiment_id=event.experiment_id, cohort=cohort
                )

        cycle = cycles.evaluate_to_settled(self._cycles, event, evaluator, before_settle=freeze)
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

    @activity.defn(name=RUN_TURN)
    def run_turn(self, intent: TurnIntent) -> TurnOutcome:
        """One turn of the LangGraph graph (ADR-0006), as one activity.

        The turn's id comes from the cycle, not from Temporal, so every attempt of this
        activity, and every later execution of this run's workflow, is the same turn:
        a rerun resumes the turn's own checkpoint and doesn't call the model a second
        time, and a turn that already finished is simply its own answer. Whatever the
        turn commits goes through the gateway with the tool's content-derived key, so
        it lands once (E1).
        """
        if self._turns is None:
            # A configuration fault, the same on every attempt: fail once, visibly, as
            # this turn's refusal, rather than spin and hold up every trigger behind it.
            raise ApplicationError(
                "this worker was built without a turn host, so it can't take turns",
                type=NO_TURN_HOST,
                non_retryable=True,
            )
        recorded = self._runs.get(intent.run_id)
        assert recorded is not None  # ensure_run ran first in this workflow
        # The stage decides which tools the turn is shown. It is the workflow's choice of
        # what the run does next (position); what any tool may commit is still layer 8's.
        run = dataclasses.replace(recorded, stage=intent.stage)
        cycle = self._cycles.get(
            TriggerEvent(
                kind=TriggerKind(intent.kind),
                experiment_id=intent.experiment_id,
                data_as_of=intent.data_as_of,
                tenant=run.tenant,
            )
        )
        assert cycle is not None  # the turn follows the cycle that settled
        turn_id = f"{intent.stage}:{intent.experiment_id}:{intent.data_as_of}:{intent.kind}"

        done = finished_turn(self._turns.deps.graph, turn_thread(run.run_id, turn_id))
        if done is not None:
            return _outcome(done)

        message = draft_instruction(tenant=run.tenant, cycle=cycle)
        with _heartbeating():
            result = take_turn(
                run=run,
                envelope=self._turns.envelope(run),
                message=message,
                deps=self._turns.deps,
                turn_id=turn_id,
            )
        self._turns.record(run, asked=message, answered=result.text)
        return TurnOutcome(
            status=result.status, receipts=len(result.receipts), refusals=len(result.refusals)
        )


def _outcome(state: dict[str, object]) -> TurnOutcome:
    receipts = state.get("receipts") or []
    refusals = state.get("refusals") or []
    assert isinstance(receipts, list) and isinstance(refusals, list)
    return TurnOutcome(
        status=str(state.get("status", "complete")),
        receipts=len(receipts),
        refusals=len(refusals),
    )


@contextmanager
def _heartbeating() -> Iterator[None]:
    """Tell Temporal this activity is alive while the turn runs.

    The turn is one blocking call, so the beat comes from a thread. The thread runs in a
    copy of this activity's context, which is how `activity.heartbeat` knows whose
    heartbeat it is. Nothing is sent with the beat: progress is the checkpoint's job.
    """
    stop = threading.Event()
    context = contextvars.copy_context()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_EVERY_S):
            context.run(activity.heartbeat)

    beating = threading.Thread(target=beat, name="turn-heartbeat", daemon=True)
    beating.start()
    try:
        yield
    finally:
        stop.set()
        beating.join()
