"""T21: many runs at once, and the guarantees that have to hold while they collide.

Everything before this was verified one run at a time, where a check-then-write can
never lose a race. These start the runs together on purpose - behind a barrier, against
the suite's own pools (app 8, checkpointer 4), which are smaller than the defaults - and
assert the properties that matter, not a timing:

* 100 runs each park, are approved, resume and commit **exactly once**, while every
  run's resuming turn is delivered twice at the same moment.
* A step raced by two workers completes once and the loser is told so, not crashed.
* A trigger batch with duplicates evaluates each experiment once, never more than the
  worker's declared activity bound at a time (T45: through a real worker, which
  replaced `fanout.py`).

Measured on a laptop (Apple silicon, Postgres 16 in Docker), recorded in tasks/todo.md:
100 runs in ~2.6s at every app pool size from 5 to 40 - one process is bound by Python,
not by Postgres - and 300 in ~9s with no pool timeout.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest
from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Worker

from agentstack.context.targeting import TargetingRule
from agentstack.execution.gateway import UnresolvedEffect
from agentstack.execution.surfaces import SurfaceRefused
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, deliver, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.policy.triggers import TriggerEvent
from agentstack.prediction.churn import ChurnScorer
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.operator import Evaluation, evaluate_trigger
from agentstack.runtime.run import Run, new_run
from agentstack.runtime.steps import StepConflict
from agentstack.runtime.temporal import activities as temporal_activities
from agentstack.runtime.temporal.contracts import RunProgress, workflow_id
from agentstack.runtime.temporal.worker import (
    MAX_CONCURRENT_ACTIVITIES,
    activity_threads,
    build_worker,
)
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.runtime.waits import ResumeEvent, resume
from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.experiments import EVALUATION_STAGE, HALT, prepare_halt
from tests.conftest import connect_temporal
from tests.fitness.test_trigger_to_candidate import ROWS, RULE, WATERMARK, StubScorer
from tests.temporal_support import activities_for, progress_until, time_skipping

RUNS = 100
TENANT = "acme"
SCOPES = frozenset({"billing:read", "billing:refund"})


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer, tenant=TENANT)


def a_run(stack: Stack, i: int) -> tuple[Run, InboundEvent]:
    """A run of its own, refunding a charge of its own - so its idempotency key is its own."""
    user = f"user-{i}"
    session = stack.resolver.start(user_id=user, tenant=TENANT)
    run = stack.runs.ensure(
        new_run(session_id=session.session_id, tenant=TENANT, user=user, channel="load")
    )
    event = InboundEvent(
        channel="load",
        tenant=TENANT,
        user_id=user,
        session_id=session.session_id,
        text=f"issue_refund tenant=acme customer_id=c-{i} charge_id=ch-{i} amount_cents=1999",
    )
    return run, event


def at_once(n: int, work: Any) -> list[BaseException]:
    """Start `n` threads behind a barrier; return whatever they raised."""
    barrier = threading.Barrier(n)
    raised: list[BaseException] = []

    def go(i: int) -> None:
        barrier.wait()
        try:
            work(i)
        except BaseException as exc:  # collected and asserted on by the caller
            raised.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "a run never finished"
    return raised


# --- 100 runs, the whole park -> approve -> resume -> commit path -----------------------


def test_a_hundred_runs_at_once_each_commit_exactly_once(
    stack: Stack, app_database: Database
) -> None:
    runs = [a_run(stack, i) for i in range(RUNS)]
    losers: list[BaseException] = []

    def lifecycle(i: int) -> None:
        run, event = runs[i]
        first = handle(stack, event, scopes=SCOPES, run=run)
        assert first.status == "awaiting_approval", first.text
        wait, request = first.pending_wait, first.pending_request
        assert wait is not None and request is not None and first.approval_summary
        stack.approvals.grant(
            run_id=run.run_id,
            request=request,
            state_snapshot=wait.state_snapshot,
            approver="finance-oncall",
            summary=first.approval_summary,
        )
        resume(
            stack.waits,
            ResumeEvent(run.run_id, wait.wait_id, wait.state_snapshot, {"approved_by": "ops"}),
        )
        # The resuming turn, delivered twice at the same moment: a channel retry that
        # arrives while the original is still in flight.
        losers.extend(at_once(2, lambda _: handle(stack, event, scopes=SCOPES, run=run)))

    app_database.pool.pop_stats()  # count this test's pool use only
    started = time.perf_counter()
    raised = at_once(RUNS, lifecycle)
    elapsed = time.perf_counter() - started
    stats = app_database.pool.get_stats()

    assert raised == [], f"{len(raised)} runs failed: {Counter(map(repr, raised)).most_common(3)}"
    # A duplicate that reaches the gateway while the original's claim is open is refused
    # as unresolved - safe, and never a second effect. Anything else is a defect.
    assert all(isinstance(exc, UnresolvedEffect) for exc in losers), Counter(map(repr, losers))

    # The charge is in the resource: `{tenant}/customers/{customer}/charges/{charge}`.
    committed = [resource for resource, _ in stack.client.calls]
    assert len(committed) == RUNS, f"{len(committed)} commits for {RUNS} runs"
    assert len(set(committed)) == RUNS, "a charge was refunded twice"
    assert stack.ledger.unresolved_keys() == (), "a claim was left unsettled"
    for run, _ in runs:
        executed = [
            r
            for r in stack.steps.records_for(run.run_id)
            if r.name.startswith("execute:") and r.status == "completed"
        ]
        assert len(executed) == 1, f"{run.run_id} completed its effect {len(executed)} times"
        assert stack.steps.started_but_unfinished(run.run_id) == ()
    assert stats.get("requests_errors", 0) == 0, f"pool timeouts under load: {stats}"
    assert elapsed < 60, f"{RUNS} runs took {elapsed:.1f}s"


# --- the step race, made deterministic --------------------------------------------------


def test_a_step_raced_by_two_workers_completes_once(stack: Stack) -> None:
    """Both pass the `completed` check before either writes. The database lets one
    completion land; the loser is told the step is done, with the winner's receipt."""
    run, _ = a_run(stack, 0)
    both_inside = threading.Barrier(2)

    def worker(_: int) -> None:
        with stack.steps.step(run.run_id, "execute:refund:fp") as slot:
            both_inside.wait()  # neither has written `completed` yet
            slot[0] = "receipt-1"

    assert at_once(2, worker) == []
    records = stack.steps.records_for(run.run_id)
    assert [r.status for r in records].count("completed") == 1
    assert stack.steps.started_but_unfinished(run.run_id) == ()


def test_two_different_effects_for_one_step_are_refused_loudly(stack: Stack) -> None:
    run, _ = a_run(stack, 0)
    both_inside = threading.Barrier(2)

    def worker(i: int) -> None:
        with stack.steps.step(run.run_id, "execute:refund:fp") as slot:
            both_inside.wait()
            slot[0] = f"receipt-{i}"

    raised = at_once(2, worker)

    assert len(raised) == 1
    assert isinstance(raised[0], StepConflict)
    assert "two effects where the step boundary promised one" in str(raised[0])


def test_only_the_complete_once_conflict_is_absorbed(stack: Stack, app_database: Database) -> None:
    """Any other refusal of the completion write is still an error. Here the run is
    deleted mid-step, so `completed` has nothing to belong to."""
    run, _ = a_run(stack, 0)

    with (
        pytest.raises(IntegrityViolation) as caught,
        stack.steps.step(run.run_id, "execute:refund:fp") as slot,
    ):
        app_database.execute("DELETE FROM runs WHERE run_id = %s", (run.run_id,))
        slot[0] = "receipt-1"

    assert caught.value.constraint == "run_steps_run_id_fkey"


# --- trigger fan-out, bounded by the worker (T45) ---------------------------------------

# Every cohort is too small to target, so every cycle abstains: an answer, and no turn
# follows it. What is under test is how many evaluations run at once, not what they say.
ABSTAINS = replace(RULE, minimum_cohort=ROWS + 1)


def payload(experiment: int) -> dict[str, str]:
    return {
        "kind": "data_arrival",
        "experiment_id": f"exp-{experiment}",
        "data_as_of": WATERMARK,
        "tenant": TENANT,
    }


class InFlight:
    """Wraps the evaluation `evaluate_cycle` runs, and counts evaluations and how many
    ran at once across the worker's threads. Slow enough that, with nothing holding them
    back, the runs' evaluations would all overlap."""

    def __init__(self, fail_for: str | None = None) -> None:
        self.fail_for = fail_for
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.evaluated: list[str] = []

    def __call__(
        self, trigger: TriggerEvent, *, scorer: ChurnScorer, rule: TargetingRule | None
    ) -> Evaluation:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.evaluated.append(trigger.experiment_id)
        try:
            time.sleep(0.2)
            if trigger.experiment_id == self.fail_for:
                raise RuntimeError("this cohort could not be scored")
            return evaluate_trigger(trigger, scorer=scorer, rule=rule)
        finally:
            with self.lock:
                self.running -= 1


@pytest.fixture
def evaluations(monkeypatch: pytest.MonkeyPatch) -> Callable[..., InFlight]:
    """Puts an `InFlight` where `evaluate_cycle` calls the operator. The worker runs its
    activities in this process, so the patch reaches them."""

    def install(fail_for: str | None = None) -> InFlight:
        counted = InFlight(fail_for)
        monkeypatch.setattr(temporal_activities, "evaluate_trigger", counted)
        return counted

    return install


@asynccontextmanager
async def roomy_worker(
    client: Client, task_queue: str, db: Database, scorer: StubScorer
) -> AsyncIterator[Worker]:
    """The production worker at its declared bound, on an executor with four times as
    many threads. Threads are then never what holds evaluations back; only the worker's
    slots can be."""
    with ThreadPoolExecutor(max_workers=4 * MAX_CONCURRENT_ACTIVITIES) as roomy:
        worker = build_worker(
            client,
            activities=activities_for(db, scorer=scorer, rule=ABSTAINS),
            executor=roomy,
            task_queue=task_queue,
        )
        async with worker:
            yield worker


def a_run_of(client: Client, run_id: str) -> WorkflowHandle[ExperimentWorkflow, Any]:
    return client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))


def settled_cycles(db: Database) -> int:
    row = db.fetch_one("SELECT count(*) FROM trigger_cycles WHERE outcome IS NOT NULL")
    assert row is not None
    return int(row[0])


@pytest.mark.usefixtures("fixture_dataset")
def test_a_hundred_triggers_with_duplicates_evaluate_each_once_within_the_bound(
    stack: Stack,
    app_database: Database,
    task_queue: str,
    evaluations: Callable[..., InFlight],
) -> None:
    """Criterion 42. 100 experiments, each delivered three times, all waiting on the
    queue before the worker starts: the stampede a batch landing makes.

    On the in-memory test server, not the dev server: the bound is the worker's, so the
    server only has to hand out tasks faster than the bound lets them run. The dev
    server's SQLite can't, for 100 runs, and the peak would then measure the server.
    """
    deliveries = [payload(i) for i in range(RUNS) for _ in range(3)]
    random.Random(21).shuffle(deliveries)
    scorer = StubScorer()
    counted = evaluations()

    async def stampede() -> tuple[int | None, list[RunProgress]]:
        async with time_skipping() as env:
            client = env.client
            run_ids = {
                await deliver(stack, client, p, source="load", task_queue=task_queue)
                for p in deliveries
            }
            async with roomy_worker(client, task_queue, app_database, scorer) as worker:
                # Watched from the record until every cycle has settled: a hundred runs'
                # queries polled at once would be workflow tasks competing with the work.
                deadline = time.monotonic() + 60
                while settled_cycles(app_database) < RUNS and time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
                progress = await asyncio.gather(
                    *(
                        progress_until(a_run_of(client, r), lambda p: len(p.cycles) == 3)
                        for r in sorted(run_ids)
                    )
                )
                return worker.config()["max_concurrent_activities"], progress

    limit, progress = asyncio.run(stampede())

    assert limit == MAX_CONCURRENT_ACTIVITIES, "the worker runs at the configured bound"
    assert len(progress) == RUNS, "one run per experiment, however often it was triggered"
    assert all({c.outcome for c in p.cycles} == {"abstain"} for p in progress)
    assert sorted(Counter(counted.evaluated).values()) == [1] * RUNS
    assert scorer.calls == RUNS
    assert counted.peak <= limit, f"{counted.peak} evaluations ran at once"
    assert counted.peak > 1, "the batch never ran concurrently; the bound is untested"
    assert settled_cycles(app_database) == RUNS, "each experiment's cycle settles once"
    assert CycleStore(db=app_database).unsettled() == ()


@pytest.mark.usefixtures("fixture_dataset")
def test_one_failed_evaluation_does_not_stop_the_batch(
    stack: Stack,
    app_database: Database,
    temporal_address: str,
    task_queue: str,
    evaluations: Callable[..., InFlight],
) -> None:
    """A failure that isn't a refusal is retried under the one policy, for as long as it
    takes. It holds a slot only while it runs, so the other runs are evaluated meanwhile."""
    counted = evaluations(fail_for="exp-3")

    async def batch() -> tuple[list[RunProgress], RunProgress]:
        client = await connect_temporal(temporal_address)
        runs = {
            f"exp-{i}": await deliver(
                stack, client, payload(i), source="load", task_queue=task_queue
            )
            for i in range(10)
        }
        async with roomy_worker(client, task_queue, app_database, StubScorer()):
            others = await asyncio.gather(
                *(
                    progress_until(a_run_of(client, run_id), lambda p: len(p.cycles) == 1)
                    for experiment, run_id in runs.items()
                    if experiment != "exp-3"
                )
            )
            # The retry policy's first interval is 1s: a second attempt is due soon after.
            deadline = time.monotonic() + 30
            while counted.evaluated.count("exp-3") < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            failing = await a_run_of(client, runs["exp-3"]).query(ExperimentWorkflow.progress)
            return others, failing

    others, failing = asyncio.run(batch())

    assert len(others) == 9
    assert all([c.outcome for c in p.cycles] == ["abstain"] for p in others)
    assert counted.evaluated.count("exp-3") >= 2, "the failed evaluation was not retried"
    assert failing.cycles == (), "a failed evaluation has no outcome to report"
    assert [c.experiment_id for c in CycleStore(db=app_database).unsettled()] == ["exp-3"], (
        "a failed evaluation must stay visible, not be recorded as an outcome"
    )


def test_the_bound_is_below_the_pool_and_is_the_one_that_binds() -> None:
    from agentstack.storage.pool import DEFAULT_MAX_SIZE

    assert 1 <= MAX_CONCURRENT_ACTIVITIES <= DEFAULT_MAX_SIZE
    never_polls = Client.__new__(Client)  # refused before the worker would touch it
    activities = activities_for(Database.__new__(Database))
    with (
        activity_threads(1) as one,
        pytest.raises(ValueError, match="would run nothing"),
    ):
        build_worker(never_polls, activities=activities, executor=one, max_concurrent_activities=0)
    with (
        activity_threads(MAX_CONCURRENT_ACTIVITIES - 1) as too_few,
        pytest.raises(ValueError, match="would bound activities below the declared"),
    ):
        build_worker(never_polls, activities=activities, executor=too_few)


# --- SPEC-registry.md criterion 4: twenty halts, one halt ------------------------------

HALTS = 20
EXPERIMENT_AT = f"{TENANT}/experiments/exp-7"
VERSION = "exp:abc123"
HALT_SCOPES = frozenset({"experiments:halt"})


def live_experiment(stack: Stack, *, rolled_out: bool = True) -> None:
    stack.registry_client.commit(
        EXPERIMENT_AT,
        {
            "experiment_version": VERSION,
            "hypothesis": "a discount retains at-risk customers",
            "variant": "20-percent-off",
        },
    )
    if not rolled_out:
        return
    stack.registry_client.commit(
        f"{EXPERIMENT_AT}/rollout",
        {
            "experiment_version": VERSION,
            "percentage": 10,
            "targeting_model_version": "tabpfn-3.5",
            "risk_threshold": 0.61,
            "prior_rollout_event": 0,
        },
    )


def halt_events(app_database: Database) -> int:
    row = app_database.fetch_one("SELECT count(*) FROM registry_events WHERE kind = 'halt'")
    assert row is not None
    return int(row[0])


def test_twenty_runs_halting_at_once_halt_once(stack: Stack, app_database: Database) -> None:
    """Twenty evaluation runs see the same guardrail breach and halt together. The key is
    business identity - one halt per version - so the ledger sees one effect however many
    runs ask; every other caller is deduplicated or told the outcome is still open."""
    live_experiment(stack)
    runs = []
    for i in range(HALTS):
        user = f"evaluator-{i}"
        session = stack.resolver.start(user_id=user, tenant=TENANT)
        run = stack.runs.ensure(
            new_run(
                session_id=session.session_id,
                tenant=TENANT,
                user=user,
                stage=EVALUATION_STAGE,
                channel="load",
                subject="exp-7",
            )
        )
        view = stack.resolver.resolve(session_id=session.session_id, user_id=user, tenant=TENANT)
        runs.append((run, envelope_for(view, scopes=HALT_SCOPES)))
    request = prepare_halt(
        {
            "tenant": TENANT,
            "experiment_id": "exp-7",
            "experiment_version": VERSION,
            "reason": "churn rose in the variant",
        }
    )
    receipts: list[str] = []

    def halt(i: int) -> None:
        run, envelope = runs[i]
        result = stack.deps.gateway.execute(
            request=request,
            spec=HALT,
            envelope=envelope,
            run_id=run.run_id,
            state_snapshot="s",
            tracer=Tracer(
                run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions
            ),
        )
        receipts.append(result.receipt)

    raised = at_once(HALTS, halt)

    assert all(isinstance(exc, UnresolvedEffect) for exc in raised), Counter(map(repr, raised))
    assert len(set(receipts)) == 1, "every caller that got an answer got the one halt"
    assert stack.registry_client.read(EXPERIMENT_AT, {})["status"] == "halted"
    assert halt_events(app_database) == 1


def test_twenty_halts_at_the_store_itself_apply_once(stack: Stack, app_database: Database) -> None:
    """The same race with the ledger out of the way: twenty different halts (different
    reasons, so different rows) straight at the registry. Only the `WHERE status =
    'live'` on the statement that moves the row stands between them and twenty events."""
    live_experiment(stack)
    receipts: list[str] = []

    def halt(i: int) -> None:
        receipts.append(
            stack.registry_client.commit(
                f"{EXPERIMENT_AT}/halt",
                {"experiment_version": VERSION, "percentage": 0, "reason": f"look {i}"},
            )
        )

    raised = at_once(HALTS, halt)

    assert len(receipts) == 1
    assert len(raised) == HALTS - 1
    assert all(isinstance(exc, SurfaceRefused) for exc in raised), Counter(map(repr, raised))
    assert halt_events(app_database) == 1


# --- audit C2: twenty moves from one state, one move ------------------------------------
#
# Each write names the state it moves from, and the statement checks it is still the
# latest. Under read committed, twenty statements that start together all see the same
# latest; the unique index on (experiment, prior) is what leaves one. Different targets
# on purpose, so no loser is the exact move on record and none may be answered with the
# winner's receipt.

MOVES = 20


def events(app_database: Database, kind: str) -> int:
    row = app_database.fetch_one("SELECT count(*) FROM registry_events WHERE kind = %s", (kind,))
    assert row is not None
    return int(row[0])


def one_landed(receipts: list[str], raised: list[BaseException]) -> None:
    assert len(receipts) == 1, receipts
    assert len(raised) == MOVES - 1
    assert all(isinstance(exc, SurfaceRefused) for exc in raised), Counter(map(repr, raised))


def test_twenty_rollouts_from_one_state_land_once(stack: Stack, app_database: Database) -> None:
    live_experiment(stack)
    prior = stack.registry_client.read(f"{EXPERIMENT_AT}/history", {})["latest_rollout_event"]
    receipts: list[str] = []

    def roll_out(i: int) -> None:
        receipts.append(
            stack.registry_client.commit(
                f"{EXPERIMENT_AT}/rollout",
                {
                    "experiment_version": VERSION,
                    "percentage": 11 + i,
                    "targeting_model_version": "tabpfn-3.5",
                    "risk_threshold": 0.61,
                    "prior_rollout_event": prior,
                },
            )
        )

    one_landed(receipts, at_once(MOVES, roll_out))
    assert events(app_database, "rollout") == 2, "the seed and one of the twenty"


def test_twenty_abstentions_against_one_read_land_once(
    stack: Stack, app_database: Database
) -> None:
    live_experiment(stack)
    prior = stack.registry_client.read(f"{EXPERIMENT_AT}/history", {})["latest_event"]
    receipts: list[str] = []

    def abstain(i: int) -> None:
        receipts.append(
            stack.registry_client.commit(
                f"{EXPERIMENT_AT}/abstention",
                {"experiment_version": VERSION, "explanation": f"look {i}", "prior_event": prior},
            )
        )

    one_landed(receipts, at_once(MOVES, abstain))
    assert events(app_database, "abstention") == 1


def test_twenty_revisions_of_one_revision_land_once(stack: Stack, app_database: Database) -> None:
    live_experiment(stack, rolled_out=False)
    receipts: list[str] = []

    def revise(i: int) -> None:
        receipts.append(
            stack.registry_client.commit(
                f"{EXPERIMENT_AT}/revision",
                {"experiment_version": VERSION, "hypothesis": f"wording {i}", "prior_revision": 1},
            )
        )

    one_landed(receipts, at_once(MOVES, revise))
    assert app_database.fetch_one("SELECT count(*) FROM draft_revisions") == (2,)
