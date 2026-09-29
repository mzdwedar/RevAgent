"""The experiment run as a Temporal workflow.

Workflow code is replayed from history on every recovery, so it must be deterministic:
no I/O, no clock, no randomness except through `workflow.*`. The sandbox catches some
of that. It does not catch an import of `execution` or `storage` (E4), which is why
contract 6 forbids them here and activities are named through `contracts`, never
imported.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from agentstack.runtime.temporal.contracts import (
    ASK_APPROVAL,
    COMMIT,
    DRAFT,
    ENSURE_RUN,
    EVALUATE_CYCLE,
    NOT_ANSWERED,
    PARK_TRIGGER_WAIT,
    REASK_EVERY,
    ROLLOUT,
    RUN_TURN,
    SATISFY_TRIGGER_WAIT,
    UNRESOLVED,
    AskIntent,
    AskResult,
    Carried,
    CommitIntent,
    CommitOutcome,
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
# One surface call and its bookkeeping.
ACT_TIMEOUT = timedelta(seconds=30)
# SPEC-durable-runtime: a run lives for weeks, and Temporal caps a workflow's history.
# Every CONTINUE_EVERY cycles the run starts a fresh history, carrying what it needs.
CONTINUE_EVERY = 100


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
        self._awaiting: str | None = None
        self._asks = 0
        self._answered: list[str] = []
        self._commits: list[CommitOutcome] = []
        self._reconciling: str | None = None
        # How many answers to each reconcile wait the run has acted on. Replay rebuilds it.
        self._spent: dict[str, int] = {}
        self._cycles_before = 0

    @workflow.run
    async def run(self, start: RunStart) -> RunEnd:
        self._run_id = start.run_id
        if start.carried is not None:
            self._take_over(start.carried)
        # The record first. Temporal knows the run exists; Postgres has to know too,
        # because every wait, step and approval hangs off the `runs` row. Every execution
        # ensures it, and after a continue-as-new it is already there.
        await workflow.execute_activity(
            ENSURE_RUN,
            replace(start, carried=None),
            start_to_close_timeout=RECORD_TIMEOUT,
            retry_policy=RETRY,
        )
        self._recorded = True
        while True:
            if len(self._cycles) >= CONTINUE_EVERY:
                await self._continue_as_new(start)
            if not self._pending:
                await self._wait_for_trigger()
            trigger = self._pending.pop(0)
            cycle = await self._evaluate(trigger)
            self._cycles.append(cycle)
            self._last_watermark = trigger.data_as_of
            if cycle.outcome == "propose":
                await self._propose(cycle)

    async def _continue_as_new(self, start: RunStart) -> None:
        """Hand the run to a fresh execution: same workflow id, same `run_id`, same record.

        Only called between cycles, which is the one point where the run holds nothing
        open: its approval and reconcile waits were answered inside `_propose`, its
        trigger wait was satisfied before the cycle ran, and no activity is in flight.
        What is left is carried. What is dropped is either in Postgres already (turns,
        commits) or spent: an answer that arrives now is for a wait nobody is on, and if
        that wait comes round again, its ask reads the answer from the row.

        A signal that lands while the handoff is being recorded is not lost: the server
        refuses the continue, the task runs again with the signal applied, and the
        trigger goes into `pending` with the rest.
        """
        await workflow.wait_condition(workflow.all_handlers_finished)
        carried = Carried(
            waits_parked=self._waits_parked,
            last_watermark=self._last_watermark,
            pending=tuple(self._pending),
            cycles_before=self._cycles_before + len(self._cycles),
        )
        workflow.continue_as_new(replace(start, carried=carried))

    def _take_over(self, carried: Carried) -> None:
        self._waits_parked = carried.waits_parked
        self._last_watermark = carried.last_watermark
        # Ahead of anything already signalled to this execution: those arrived later.
        self._pending[:0] = carried.pending
        self._cycles_before = carried.cycles_before

    async def _propose(self, cycle: CycleResult) -> None:
        """SPEC.md: draft the experiment, then ask a person before rolling it out.

        Drafting is the model's job, in a turn, and policy lets it commit (PRE_COMMIT).
        The rollout is proposed in a second turn, which the gateway stops at the
        approval boundary (ALWAYS): the turn parks the wait and the run asks.
        """
        drafted = await self._settled_turn(DRAFT, cycle)
        if drafted.refusal is not None or drafted.receipts == 0:
            return
        proposed = await self._settled_turn(ROLLOUT, cycle)
        if proposed.wait_id is not None and cycle.experiment_version is not None:
            wait_id = proposed.wait_id
            await self._await_approval(wait_id, cycle)
            while not await self._act(wait_id, cycle):
                # Woken, and the wait says nobody answered: a stray or forged signal. The
                # wake is spent, and the run goes back to the question it was on. The
                # answer that does come later wakes it again, and still counts.
                self._answered.remove(wait_id)
                await self._await_approval(wait_id, cycle, asked=True)

    async def _settled_turn(self, stage: str, cycle: CycleResult) -> TurnOutcome:
        """Take the turn; if it met an effect of unknown outcome, wait for the claim to be
        settled and take the same turn again (M1).

        The turn parked a reconcile wait and stopped with its checkpoint before `act`, so
        taking it again resumes there: the proposals already made are not asked for
        again, and the ledger answers for the one that was unknown.
        """
        while True:
            outcome = await self._take_turn(stage, cycle)
            self._turns.append(outcome)
            if outcome.status != UNRESOLVED or outcome.wait_id is None:
                return outcome
            await self._reconciled(outcome.wait_id)

    async def _reconciled(self, wait_id: str) -> None:
        """Wait until someone has settled this claim and said so (`operator reconcile`).

        Each answer is spent once. An act that comes back unresolved on the same wait
        again (a wake-up nobody settled anything for) waits for a new answer instead of
        going round again at once: the gateway would only refuse it again, and a run
        spinning on a claim hides it rather than surfacing it.
        """
        spent = self._spent.get(wait_id, 0)
        self._reconciling = wait_id
        await workflow.wait_condition(lambda: self._answered.count(wait_id) > spent)
        self._spent[wait_id] = spent + 1
        self._reconciling = None

    async def _act(self, wait_id: str, cycle: CycleResult) -> bool:
        """The answer is in: commit what it was about. Whether it may happen is the
        gateway's to decide at the act, against the world as it is then. False if the
        wait turned out to be unanswered: the run acted on nothing, and must wait again.

        An effect of unknown outcome parks the run until someone reconciles it against
        the surface, then acts again, and the ledger makes that a deduplication. It is
        never retried blind (E2).
        """
        intent = CommitIntent(
            run_id=self._run_id,
            wait_id=wait_id,
            experiment_id=cycle.experiment_id,
            data_as_of=cycle.data_as_of,
            kind=cycle.kind,
        )
        while True:
            outcome = await self._commit(intent)
            self._commits.append(outcome)
            if outcome.status == NOT_ANSWERED:
                return False
            if outcome.status != UNRESOLVED or outcome.wait_id is None:
                return True
            await self._reconciled(outcome.wait_id)

    async def _commit(self, intent: CommitIntent) -> CommitOutcome:
        try:
            outcome: CommitOutcome = await workflow.execute_activity(
                COMMIT,
                intent,
                result_type=CommitOutcome,
                start_to_close_timeout=ACT_TIMEOUT,
                retry_policy=RETRY,
            )
            return outcome
        except ActivityError as exc:
            # A refusal is the act's answer: stale, unapproved, or refused by the surface.
            # It was audited where it was decided, and asking again can't change it.
            cause = exc.cause
            refusal = cause.type if isinstance(cause, ApplicationError) else None
            return CommitOutcome(status="refused", refusal=refusal or type(cause).__name__)

    async def _await_approval(
        self, wait_id: str, cycle: CycleResult, *, asked: bool = False
    ) -> None:
        """Ask, then wait. Every REASK_EVERY without an answer, ask again. Never expire.

        The wait row says what is asked and against which snapshot; the answer is
        authorised and recorded by layer 8 before the workflow is told (T42). This
        only decides when to put the question again. `asked`: the question is already
        out (the run is back after a wake nobody answered), so wait before putting it
        again, rather than letting every stray signal post it once more.
        """
        assert cycle.experiment_version is not None
        self._awaiting = wait_id
        if not asked:
            self._asks = 0
        while True:
            if not asked:
                await self._ask(wait_id, cycle.experiment_id, cycle.experiment_version)
            asked = False
            try:
                await workflow.wait_condition(
                    lambda: wait_id in self._answered, timeout=REASK_EVERY
                )
                break
            except TimeoutError:
                continue  # unanswered: the question is put again, never dropped
        self._awaiting = None

    async def _ask(self, wait_id: str, experiment_id: str, experiment_version: str) -> None:
        intent = AskIntent(
            run_id=self._run_id,
            wait_id=wait_id,
            experiment_id=experiment_id,
            experiment_version=experiment_version,
            asked=self._asks,
        )
        try:
            result: AskResult = await workflow.execute_activity(
                ASK_APPROVAL,
                intent,
                result_type=AskResult,
                start_to_close_timeout=RECORD_TIMEOUT,
                retry_policy=RETRY,
            )
        except ActivityError:
            # Only a refusal gets here (a worker that cannot ask at all). The wait stays
            # pending and visible, and the next interval tries again.
            result = AskResult(answered=False)
        self._asks += 1
        if result.answered and wait_id not in self._answered:
            # The answer was recorded but its signal never came: the timer caught it.
            self._answered.append(wait_id)

    @workflow.signal
    def answered(self, wait_id: str) -> None:
        """A wait this run parked was satisfied: a person's answer, authorised and
        recorded by layer 8 in the callback, or an unresolved effect reconciled.

        Carries a wait id and nothing else: whether it was a yes, and who said it, is in
        the `approvals` row and the wait, where layer 8 wrote it. Nothing here decides.
        A wake-up nobody answered finds the wait unsatisfied at the act, which does
        nothing and says so, and the run waits again; a "no" or an outsider's approval
        meets the gateway's refusal.
        """
        self._answered.append(wait_id)

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
            awaiting_approval=self._awaiting,
            asks=self._asks,
            answered=tuple(self._answered),
            commits=tuple(self._commits),
            reconciling=self._reconciling,
            cycles_before=self._cycles_before,
        )
