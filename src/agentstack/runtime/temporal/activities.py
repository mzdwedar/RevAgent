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

from agentstack.context.frozen_cohorts import FrozenCohort, FrozenCohortStore
from agentstack.context.targeting import TargetingRule
from agentstack.execution.gateway import UnresolvedEffect
from agentstack.observability.spans import SpanSink, Tracer
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.policy.triggers import Outcome, TriggerEvent, TriggerKind
from agentstack.prediction.churn import ChurnScorer
from agentstack.runtime import cycles
from agentstack.runtime.drafting import ROLLOUT_PERCENTAGE, draft_instruction, rollout_instruction
from agentstack.runtime.graph import finished_turn, turn_thread
from agentstack.runtime.loop import TurnDeps
from agentstack.runtime.loop import run_turn as take_turn
from agentstack.runtime.operator import Evaluation, evaluate_trigger
from agentstack.runtime.run import Run, RunStore
from agentstack.runtime.snapshot import world_snapshot
from agentstack.runtime.temporal.contracts import (
    ASK_APPROVAL,
    COMMIT,
    DRAFT,
    ENSURE_RUN,
    EVALUATE_CYCLE,
    PARK_TRIGGER_WAIT,
    ROLLOUT,
    RUN_TURN,
    SATISFY_TRIGGER_WAIT,
    AskIntent,
    AskResult,
    CommitIntent,
    CommitOutcome,
    CycleResult,
    ParkedWait,
    RunStart,
    Trigger,
    TriggerArrived,
    TriggerWaitIntent,
    TurnIntent,
    TurnOutcome,
)
from agentstack.runtime.waits import (
    APPROVAL_REASK_AFTER,
    RECONCILE,
    TRIGGER,
    ResumeEvent,
    Wait,
    WaitStore,
    reconcile_wait_id,
    resume,
)
from agentstack.tools.action import ActionRequest
from agentstack.tools.registry import ToolNotExposed
from agentstack.tools.spec import ToolSpec
from agentstack.tools.validation import InvalidToolArguments

# A turn's p95 is 60s (SPEC.md), and the workflow gives it 120s. A heartbeat every few
# seconds lets Temporal notice a dead worker in `HEARTBEAT_TIMEOUT` instead of waiting
# out the full two minutes.
HEARTBEAT_EVERY_S = 5.0

NO_TURN_HOST = "NoTurnHost"
NO_ASKER = "NoAsker"
# The answered wait names an action no checkpointed proposal of its turn prepares to.
NOTHING_APPROVED = "NothingApproved"
# The surface can't describe the resource, so no approval can be checked against it now.
WORLD_UNREADABLE = "WorldUnreadable"


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


class Asker(Protocol):
    """Puts an approval question to a person. Supplied by the composition root: the
    runtime can't import the channel (contract 4) and shouldn't know it's Slack.

    Everything the question says comes from the record (the wait and the frozen cohort),
    so the process that asks needn't be the one that parked it. Returns the message id.
    """

    def ask(self, *, run: Run, wait: Wait, cohort: FrozenCohort, percentage: int) -> str: ...


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
        traces: SpanSink,
        rule: TargetingRule | None = None,
        turns: TurnHost | None = None,
        asker: Asker | None = None,
    ) -> None:
        # Required: an activity's caller is the workflow, which never holds spans (they
        # would be history). Without a sink, every span a turn or a commit emits is lost.
        self._traces = traces
        self._asker = asker
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
        turns = self._host()
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
        turn_id = _turn_id(intent.stage, intent.experiment_id, intent.data_as_of, intent.kind)

        done = finished_turn(turns.deps.graph, turn_thread(run.run_id, turn_id))
        if done is not None:
            return _outcome(done)

        message = self._instruction(intent, run, cycle)
        with _heartbeating():
            result = take_turn(
                run=run,
                envelope=turns.envelope(run),
                message=message,
                deps=turns.deps,
                turn_id=turn_id,
            )
        # Tagged with this run and its session, as every span is: never with Temporal's ids.
        self._traces.export(result.tracer.spans)
        turns.record(run, asked=message, answered=result.text)
        return TurnOutcome(
            status=result.status,
            receipts=len(result.receipts),
            refusals=len(result.refusals),
            wait_id=None if result.pending_wait is None else result.pending_wait.wait_id,
        )

    def _host(self) -> TurnHost:
        if self._turns is None:
            # A configuration fault, the same on every attempt: fail once, visibly, as
            # this act's refusal, rather than spin and hold up every trigger behind it.
            raise ApplicationError(
                "this worker was built without a turn host, so it can't take turns or act",
                type=NO_TURN_HOST,
                non_retryable=True,
            )
        return self._turns

    def _instruction(self, intent: TurnIntent, run: Run, cycle: cycles.Cycle) -> str:
        """What this stage's turn is told, from the record. Never passed through history."""
        if intent.stage == DRAFT:
            return draft_instruction(tenant=run.tenant, cycle=cycle)
        return rollout_instruction(
            experiment_id=intent.experiment_id, cohort=self._frozen(run.tenant, cycle)
        )

    def _frozen(self, tenant: str, cycle: cycles.Cycle) -> FrozenCohort:
        assert cycle.run_id is not None  # a proposed cycle names its experiment version
        cohort = self._cohorts.get(
            tenant=tenant, experiment_id=cycle.experiment_id, experiment_version=cycle.run_id
        )
        assert cohort is not None  # recorded before the cycle that proposed it settled
        return cohort

    @activity.defn(name=ASK_APPROVAL)
    def ask_approval(self, intent: AskIntent) -> AskResult:
        """Put the question to a person, from the record, and note that it was put.

        The first ask and every re-ask go through here. Asking twice is harmless (the
        second answer to one wait is refused as already answered); a question never put
        is the silent expiry this exists to prevent. So the question is posted first and
        the re-ask recorded after, only moving forward, and an answer that landed while
        it was being put wins.
        """
        if self._asker is None:
            raise ApplicationError(
                "this worker was built without an asker, so no one can be asked",
                type=NO_ASKER,
                non_retryable=True,
            )
        run = self._runs.get(intent.run_id)
        wait = self._waits.get(intent.wait_id)
        assert run is not None and wait is not None  # parked by this run's turn
        if wait.satisfied:
            return AskResult(answered=True)  # answered before it could be asked again
        cohort = self._cohorts.get(
            tenant=run.tenant,
            experiment_id=intent.experiment_id,
            experiment_version=intent.experiment_version,
        )
        assert cohort is not None  # the rollout was proposed from it
        self._asker.ask(run=run, wait=wait, cohort=cohort, percentage=ROLLOUT_PERCENTAGE)
        if intent.asked > 0:
            self._waits.record_asked(
                intent.wait_id,
                reasks=intent.asked,
                next_deadline=datetime.now(UTC) + APPROVAL_REASK_AFTER,
            )
        return AskResult(answered=False)

    @activity.defn(name=COMMIT)
    def commit(self, intent: CommitIntent) -> CommitOutcome:
        """The irreversible act: what the person was asked about, through the gateway.

        Nothing is carried to here but ids (ADR-0008 rule 3). The action is the one the
        rollout turn proposed, read from its checkpoint and held to the fingerprint the
        wait recorded. The snapshot is the world as the surface describes it *now*, so
        an approval given against a world that has since moved is `ApprovalStale`. The
        envelope is minted for this act. Whether it may happen at all is the gateway's
        to decide: a "no", an outsider's click or a forged wake-up leaves no human
        approval, and the gateway refuses. That refusal is audited and not retried.

        An effect of unknown outcome is not retried either (E2). The run parks a
        reconcile wait, and once someone has settled the claim against the surface, the
        rerun deduplicates.
        """
        turns = self._host()
        recorded = self._runs.get(intent.run_id)
        wait = self._waits.get(intent.wait_id)
        assert recorded is not None and wait is not None  # parked by this run's turn
        run = dataclasses.replace(recorded, stage=ROLLOUT)
        request, spec = self._approved(run, intent, wait)
        tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=turns.deps.versions)
        # Exported however the act ends: a refusal is the trace someone will want most.
        try:
            return self._act(turns, run, request, spec, tracer)
        finally:
            self._traces.export(tracer.spans)

    def _act(
        self, turns: TurnHost, run: Run, request: ActionRequest, spec: ToolSpec, tracer: Tracer
    ) -> CommitOutcome:
        gateway = turns.deps.gateway
        world = gateway.observe(request=request, tracer=tracer)
        if world is None:
            raise ApplicationError(
                f"{request.resource} can't be described by its surface now, so no approval "
                "can be checked against it at the act",
                type=WORLD_UNREADABLE,
                non_retryable=True,
            )
        try:
            result = gateway.execute(
                request=request,
                spec=spec,
                envelope=turns.envelope(run),
                run_id=run.run_id,
                state_snapshot=world_snapshot(request.resource, world),
                tracer=tracer,
            )
        except UnresolvedEffect:
            parked = self._waits.park(
                wait_id=reconcile_wait_id(run.run_id, request.fingerprint()),
                run_id=run.run_id,
                kind=RECONCILE,
                state_snapshot=world_snapshot(request.resource, world),
                action_fingerprint=request.fingerprint(),
            )
            return CommitOutcome(status="unresolved", wait_id=parked.wait_id)
        return CommitOutcome(status="deduplicated" if result.deduplicated else "committed")

    def _approved(
        self, run: Run, intent: CommitIntent, wait: Wait
    ) -> tuple[ActionRequest, ToolSpec]:
        """The proposal this wait asked about, as the rollout turn checkpointed it.

        Prepared again, against what this run may use now, and held to the fingerprint
        the wait recorded: what commits is what the person was shown, or nothing.
        """
        deps = self._host().deps
        turn_id = _turn_id(ROLLOUT, intent.experiment_id, intent.data_as_of, intent.kind)
        state = finished_turn(deps.graph, turn_thread(run.run_id, turn_id)) or {}
        proposals = state.get("proposals") or []
        assert isinstance(proposals, list)
        exposed = deps.registry.expose_for(tenant=run.tenant, stage=run.stage)
        for proposal in proposals:
            try:
                request = deps.registry.prepare(
                    str(proposal["tool"]), proposal["arguments"], exposed=exposed
                )
            except (InvalidToolArguments, ToolNotExposed):
                continue
            if request.fingerprint() == wait.action_fingerprint:
                return request, deps.registry.spec(request.tool)
        raise ApplicationError(
            f"{wait.wait_id} asked about an action its turn never proposed; nothing to commit",
            type=NOTHING_APPROVED,
            non_retryable=True,
        )


def _turn_id(stage: str, experiment_id: str, data_as_of: str, kind: str) -> str:
    """A turn's identity comes from the cycle, not from Temporal (E1)."""
    return f"{stage}:{experiment_id}:{data_as_of}:{kind}"


def _outcome(state: dict[str, object]) -> TurnOutcome:
    receipts = state.get("receipts") or []
    refusals = state.get("refusals") or []
    assert isinstance(receipts, list) and isinstance(refusals, list)
    wait_id = state.get("wait_id")
    return TurnOutcome(
        status=str(state.get("status", "complete")),
        receipts=len(receipts),
        refusals=len(refusals),
        # A finished turn that stopped for a person reports the same wait on a rerun.
        wait_id=None if wait_id is None else str(wait_id),
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
