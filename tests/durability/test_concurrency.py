"""T21: many runs at once, and the guarantees that have to hold while they collide.

Everything before this was verified one run at a time, where a check-then-write can
never lose a race. These start the runs together on purpose - behind a barrier, against
the suite's own pools (app 8, checkpointer 4), which are smaller than the defaults - and
assert the properties that matter, not a timing:

* 100 runs each park, are approved, resume and commit **exactly once**, while every
  run's resuming turn is delivered twice at the same moment.
* A step raced by two workers completes once and the loser is told so, not crashed.
* A trigger batch with duplicates evaluates each experiment once, never more than
  `max_in_flight` at a time.

Measured on a laptop (Apple silicon, Postgres 16 in Docker), recorded in tasks/todo.md:
100 runs in ~2.6s at every app pool size from 5 to 40 - one process is bound by Python,
not by Postgres - and 300 in ~9s with no pool timeout.
"""

from __future__ import annotations

import random
import threading
import time
from collections import Counter
from typing import Any

import pytest

from agentstack.execution.gateway import UnresolvedEffect
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.triggers import parse_trigger
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.policy.triggers import Outcome, TriggerEvent
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.fanout import DEFAULT_MAX_IN_FLIGHT, fan_out
from agentstack.runtime.run import Run, new_run
from agentstack.runtime.steps import StepConflict
from agentstack.runtime.waits import ResumeEvent, resume
from agentstack.storage.database import Database, IntegrityViolation

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


# --- trigger fan-out --------------------------------------------------------------------


def trigger(experiment: int) -> TriggerEvent:
    return parse_trigger(
        {
            "kind": "data_arrival",
            "experiment_id": f"exp-{experiment}",
            "data_as_of": "telecom-bigml:f107d488f7bf4651",
            "tenant": TENANT,
        },
        source="load",
    )


class Evaluator:
    """Counts evaluations and how many ran at once. Slow enough that they overlap."""

    def __init__(self, fail_for: str | None = None) -> None:
        self.fail_for = fail_for
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.evaluated: list[str] = []

    def __call__(self, event: TriggerEvent) -> tuple[Outcome, str | None]:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.evaluated.append(event.experiment_id)
        try:
            time.sleep(0.02)
            if event.experiment_id == self.fail_for:
                raise RuntimeError("this cohort could not be scored")
            return Outcome.CONTINUE, None
        finally:
            with self.lock:
                self.running -= 1


def test_a_batch_with_duplicates_evaluates_each_experiment_once_within_the_bound(
    app_database: Database,
) -> None:
    """100 experiments, each delivered three times, all at once."""
    deliveries = [trigger(i) for i in range(RUNS) for _ in range(3)]
    random.Random(21).shuffle(deliveries)
    evaluator = Evaluator()

    results = fan_out(CycleStore(db=app_database), deliveries, evaluator, max_in_flight=8)

    assert len(results) == 3 * RUNS
    assert [r.error for r in results if r.error] == []
    assert sorted(Counter(evaluator.evaluated).values()) == [1] * RUNS
    assert evaluator.peak <= 8, f"{evaluator.peak} evaluations ran at once"
    assert evaluator.peak > 1, "the batch never ran concurrently; the bound is untested"


def test_one_failed_evaluation_does_not_stop_the_batch(app_database: Database) -> None:
    store = CycleStore(db=app_database)
    evaluator = Evaluator(fail_for="exp-3")

    results = fan_out(store, [trigger(i) for i in range(10)], evaluator, max_in_flight=4)

    failed = [r for r in results if r.error is not None]
    assert [r.trigger.experiment_id for r in failed] == ["exp-3"]
    assert all(r.cycle is not None for r in results if r.error is None)
    assert [c.experiment_id for c in store.unsettled()] == ["exp-3"], (
        "a failed evaluation must stay visible, not be recorded as an outcome"
    )


def test_the_bound_is_below_the_pool_and_must_admit_something(app_database: Database) -> None:
    from agentstack.storage.pool import DEFAULT_MAX_SIZE

    assert 1 <= DEFAULT_MAX_IN_FLIGHT <= DEFAULT_MAX_SIZE
    with pytest.raises(ValueError, match="would evaluate nothing"):
        fan_out(CycleStore(db=app_database), [trigger(0)], Evaluator(), max_in_flight=0)
