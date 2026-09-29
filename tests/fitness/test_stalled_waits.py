"""Criterion 4: a stalled run surfaces. And an unanswered approval is asked again.

A run that is waiting is indistinguishable from a run that is stuck unless something
asks. These tests hold the two halves of that: nothing that can go unanswered forever
is parked without a deadline, and past the deadline it is either reported (a trigger
that never came) or put again (a question nobody answered) - never left to lapse.

Time is an argument, not a sleep. "A week later" is `now + 7d`.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentstack.context.frozen_cohorts import FrozenCohort, FrozenCohortStore
from agentstack.context.targeting import Cohort, TargetingRule
from agentstack.interfaces import operator_cli
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.slack import RecordingNotifier
from agentstack.interfaces.triggers import parse_trigger
from agentstack.interfaces.wiring import ChannelAsker, ExperimentTurns, Stack, build_stack, handle
from agentstack.runtime.cycles import UNSETTLED_AFTER, CycleStore
from agentstack.runtime.drafting import intended_rollout
from agentstack.runtime.run import Run
from agentstack.runtime.temporal.contracts import AskIntent
from agentstack.runtime.waits import (
    APPROVAL_REASK_AFTER,
    RECONCILE,
    RECONCILE_DUE_AFTER,
    ResumeEvent,
    Wait,
    WaitWithoutDeadline,
    park_reconcile,
    resume,
)
from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.action import ActionRequest
from agentstack.tools.experiments import ROLLOUT, prepare_rollout
from tests.temporal_support import activities_for

from .conftest import SCOPES, TENANT

WEEK = timedelta(days=7)


def later(delta: timedelta) -> datetime:
    return datetime.now(UTC) + delta


def park_trigger(stack: Stack, run: Run, timeout: timedelta = WEEK) -> Wait:
    return stack.waits.park(
        run_id=run.run_id, kind="trigger", state_snapshot="fp-1", timeout=timeout
    )


def park_approval(
    stack: Stack, run: Run, *, summary: str | None = "roll_out_variant_to_percentage — IRREVERSIBLE"
) -> Wait:
    """What a rollout turn parks: the frozen cohort's rollout, its fingerprint and the
    action itself (A1: the wait holds what it asks about, and an ask reads it from there)."""
    cohort = frozen(stack, run)
    # `ask_approval` reads the registry's own prior_rollout_event (C2) at ask time, so
    # the experiment this wait is about has to actually be there to read.
    stack.registry_client.commit(
        f"{run.tenant}/experiments/exp-7",
        {"experiment_version": VERSION, "hypothesis": "a discount retains", "variant": "20-off"},
    )
    arguments = intended_rollout(cohort, prior_rollout_event=0)
    return stack.waits.park(
        run_id=run.run_id,
        kind="human_approval",
        state_snapshot="fp-1",
        action_fingerprint=prepare_rollout(arguments).fingerprint(),
        approval_summary=summary,
        action_tool=ROLLOUT.name,
        action_arguments=arguments,
    )


# --- every wait that can go unanswered forever has a deadline --------------------------


def test_a_trigger_wait_without_a_deadline_is_refused(stack: Stack, run: Run) -> None:
    with pytest.raises(WaitWithoutDeadline, match="trigger wait needs a deadline"):
        stack.waits.park(run_id=run.run_id, kind="trigger", state_snapshot="fp-1")

    assert stack.waits.pending_for(run.run_id) == ()


def test_the_database_refuses_one_too(app_database: Database, run: Run) -> None:
    """Held below the store as well, so a second writer cannot forget it."""
    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(
            "INSERT INTO waits (wait_id, run_id, kind, state_snapshot, created_at)"
            " VALUES ('wait-raw', %s, 'trigger', 'fp-1', now())",
            (run.run_id,),
        )

    assert caught.value.constraint == "pending_waits_have_a_deadline"


@pytest.mark.parametrize("timeout", [timedelta(0), timedelta(seconds=-1)])
def test_a_deadline_already_past_when_parked_is_refused(
    stack: Stack, run: Run, timeout: timedelta
) -> None:
    with pytest.raises(WaitWithoutDeadline, match="cannot be due"):
        park_trigger(stack, run, timeout=timeout)


def test_a_parked_approval_is_due_to_be_asked_again(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    """The real path, not a hand-parked row: the turn that stops for a human sets it."""
    before = datetime.now(UTC)
    result = handle(stack, event, scopes=SCOPES, run=run)

    assert result.status == "awaiting_approval"
    (wait,) = stack.waits.pending_for(run.run_id)
    assert wait.deadline is not None
    assert before + APPROVAL_REASK_AFTER <= wait.deadline <= later(APPROVAL_REASK_AFTER)


# --- a trigger that never came is reported, not left asleep ---------------------------


def test_a_trigger_wait_past_its_deadline_is_stalled(stack: Stack, run: Run) -> None:
    wait = park_trigger(stack, run)

    assert stack.waits.stalled(now=later(WEEK - timedelta(minutes=1))) == ()
    assert [w.wait_id for w in stack.waits.stalled(now=later(WEEK + timedelta(minutes=1)))] == [
        wait.wait_id
    ]


def test_a_satisfied_trigger_wait_is_not_stalled(stack: Stack, run: Run) -> None:
    wait = park_trigger(stack, run)
    resume(stack.waits, ResumeEvent(run.run_id, wait.wait_id, "fp-1", {"trigger": "arrived"}))

    assert stack.waits.stalled(now=later(2 * WEEK)) == ()


def test_an_overdue_approval_is_reasked_not_reported_as_stalled(stack: Stack, run: Run) -> None:
    """Different remedies: nobody can make data arrive, but a person can be asked again."""
    park_approval(stack, run)

    assert stack.waits.stalled(now=later(2 * WEEK)) == ()


def test_older_than_narrows_the_report_and_never_widens_it(stack: Stack, run: Run) -> None:
    park_trigger(stack, run, timeout=timedelta(hours=1))
    now = later(timedelta(days=2))

    assert len(stack.waits.stalled(now=now, older_than=timedelta(days=1))) == 1
    assert stack.waits.stalled(now=now, older_than=timedelta(days=3)) == ()
    # Old enough, but not yet due: the deadline decides, not the age.
    park_trigger(stack, run, timeout=timedelta(days=30))
    assert len(stack.waits.stalled(now=now)) == 1


def test_a_stalled_wait_survives_the_process_that_parked_it(
    stack: Stack, run: Run, app_database: Database, checkpointer: Any
) -> None:
    """The point of the deadline is that nobody has to remember it in memory."""
    wait = park_trigger(stack, run)

    restarted = build_stack(app_database, checkpointer, tenant=TENANT)

    assert [w.wait_id for w in restarted.waits.stalled(now=later(2 * WEEK))] == [wait.wait_id]


def test_operator_stalled_reports_it_and_exits_nonzero(
    stack: Stack, run: Run, app_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    wait = park_trigger(stack, run)

    code = operator_cli.main(
        ["--url", app_database_url, "stalled", "--older-than", "7d"], now=later(timedelta(days=8))
    )

    out = capsys.readouterr().out
    assert code == 1
    assert wait.wait_id in out and run.run_id in out and f"tenant {TENANT}" in out
    assert "overdue 1d0h" in out
    assert "1 stalled" in out


def test_operator_stalled_reports_a_cycle_that_never_settled(
    app_database: Database, app_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Migration 0005 says `operator stalled` works through unsettled cycles; until
    Checkpoint J nothing did. A cycle claimed long ago and never settled was refused
    by layer 8, or is stuck retrying. Either way a person should hear about it."""
    CycleStore(db=app_database).claim(
        parse_trigger(
            {
                "kind": "metric_movement",
                "experiment_id": "exp-7",
                "data_as_of": "telecom:abc",
                "tenant": TENANT,
            },
            source="test",
        )
    )

    code = operator_cli.main(
        ["--url", app_database_url, "stalled"], now=later(UNSETTLED_AFTER + timedelta(minutes=1))
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "unsettled  exp-7 @ telecom:abc  kind metric_movement" in out
    assert "1 unsettled" in out


def test_a_cycle_still_being_evaluated_is_not_reported(
    app_database: Database, app_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every cycle is unsettled while it scores. That is work in progress, not a stall."""
    CycleStore(db=app_database).claim(
        parse_trigger(
            {
                "kind": "data_arrival",
                "experiment_id": "exp-7",
                "data_as_of": "telecom:abc",
                "tenant": TENANT,
            },
            source="test",
        )
    )

    code = operator_cli.main(["--url", app_database_url, "stalled"], now=later(timedelta(0)))

    assert code == 0
    assert "0 unsettled" in capsys.readouterr().out


def test_operator_stalled_is_quiet_when_nothing_is(
    stack: Stack, run: Run, app_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    park_trigger(stack, run)

    code = operator_cli.main(["--url", app_database_url, "stalled"], now=later(timedelta(hours=1)))

    assert code == 0
    assert "0 stalled" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90s", timedelta(seconds=90)),
        ("15m", timedelta(minutes=15)),
        ("6h", timedelta(hours=6)),
        ("7d", WEEK),
    ],
)
def test_durations_name_their_unit(text: str, expected: timedelta) -> None:
    assert operator_cli.duration(text) == expected


@pytest.mark.parametrize("text", ["7", "7w", "-1d", "d"])
def test_a_duration_without_a_known_unit_is_refused(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="not a duration"):
        operator_cli.duration(text)


def test_overdue_formats_hours_below_a_day() -> None:
    assert operator_cli._ago(timedelta(hours=5, minutes=59)) == "5h"


# --- an effect of unknown outcome is watched, and a person is told to settle it --------
#
# Audit finding H5. A reconcile wait had no deadline (0010's CHECK named only trigger and
# approval waits), named no claim, and `stalled` keyed on deadlines, so it never said a
# word. The run blocked indefinitely, and so did every trigger queued behind it.


def park_reconcile_wait(stack: Stack, run: Run) -> Wait:
    return park_reconcile(
        stack.waits,
        run_id=run.run_id,
        action_fingerprint="fp-rollout",
        idempotency_key="rollout:acme:exp-7:v1:10",
        claimed_at=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
        state_snapshot="world-1",
    )


def test_a_reconcile_wait_is_parked_due_by_a_deadline(stack: Stack, run: Run) -> None:
    before = datetime.now(UTC)
    wait = park_reconcile_wait(stack, run)

    assert wait.kind == RECONCILE and wait.idempotency_key == "rollout:acme:exp-7:v1:10"
    assert wait.deadline is not None
    assert before + RECONCILE_DUE_AFTER <= wait.deadline <= later(RECONCILE_DUE_AFTER)


@pytest.mark.parametrize(
    ("columns", "values", "constraint"),
    [
        (
            "action_fingerprint, idempotency_key",
            "'fp-rollout', 'rollout:acme:exp-7:v1:10'",
            "pending_reconcile_waits_have_a_deadline",
        ),
        ("action_fingerprint, deadline", "'fp-rollout', now()", "reconcile_waits_name_their_claim"),
        ("idempotency_key, deadline", "'k', now()", "reconcile_waits_name_their_claim"),
    ],
)
def test_the_database_refuses_a_reconcile_wait_nobody_could_act_on(
    app_database: Database, run: Run, columns: str, values: str, constraint: str
) -> None:
    """Held below the store too (migrations/0019): no deadline, or no claim to settle."""
    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(
            f"INSERT INTO waits (wait_id, run_id, kind, state_snapshot, created_at, {columns})"
            f" VALUES ('wait-raw', %s, 'reconcile', 's', now(), {values})",
            (run.run_id,),
        )

    assert caught.value.constraint == constraint


def test_a_reconcile_wait_is_reported_from_the_moment_it_parks(stack: Stack, run: Run) -> None:
    """Nothing settles it but a person, so it is news at once, like a migration."""
    wait = park_reconcile_wait(stack, run)

    assert [w.wait_id for w in stack.waits.stalled(now=later(timedelta(minutes=1)))] == [
        wait.wait_id
    ]


def test_operator_stalled_names_the_reconcile_wait_and_how_to_settle_it(
    stack: Stack, run: Run, app_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    wait = park_reconcile_wait(stack, run)

    code = operator_cli.main(["--url", app_database_url, "stalled"], now=later(timedelta(0)))

    out = capsys.readouterr().out
    assert code == 1, "a scheduler alerts on the exit code"
    assert f"  reconcile  {wait.wait_id}  run {run.run_id}  tenant {TENANT}" in out
    assert "claim rollout:acme:exp-7:v1:10" in out
    assert f"settle: agentstack-operator reconcile {wait.wait_id}" in out
    assert "OVERDUE" not in out
    assert "0 stalled, 0 needs_migration, 1 reconcile, 0 unsettled" in out


def test_past_its_deadline_a_reconcile_wait_is_overdue(
    stack: Stack, run: Run, app_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    park_reconcile_wait(stack, run)

    code = operator_cli.main(
        ["--url", app_database_url, "stalled"],
        now=later(RECONCILE_DUE_AFTER + timedelta(hours=2)),
    )

    assert code == 1
    assert "OVERDUE 2h" in capsys.readouterr().out


# --- an unanswered approval is asked again, never silently expired --------------------


# Re-pointed at T41. The timer used to be `deadlines.fire_reasks`, a sweep over overdue
# rows; it is now the run's own workflow, which asks every REASK_EVERY until answered
# (`tests/durability/test_approval_wait.py` holds that across 72 simulated hours). What
# these hold is the ask itself, the `ask_approval` activity: the same five guarantees.

VERSION = "exp:abc123"


def frozen(stack: Stack, run: Run) -> FrozenCohort:
    """The cohort the question is about: an ask is sized from it, never from memory."""
    rule = TargetingRule(
        profile="test", risk_quantile=0.9, minimum_cohort=10, minimum_annual_value_at_risk_cents=1
    )
    return FrozenCohortStore(db=stack.runs.db).record(
        tenant=run.tenant,
        experiment_id="exp-7",
        cohort=Cohort(
            experiment_version=VERSION,
            dataset="fixture",
            data_as_of="fixture:abc",
            model_version="stub-1",
            rule=rule,
            risk_threshold=0.61,
            members=tuple(range(40)),
            annual_value_at_risk_cents=1_000_000,
            risk_quantiles=(0.1, 0.3, 0.5, 0.7, 0.9),
            revenue_note="fixture",
        ),
    )


class Asking:
    """Records every question put, and can fail or be answered mid-ask."""

    def __init__(self, stack: Stack, *, fail: bool = False, answer: bool = False) -> None:
        self.inner = ChannelAsker(stack.notifier)
        self.stack = stack
        self.fail = fail
        self.answer = answer
        self.asked: list[str] = []

    def ask(self, *, run: Run, wait: Wait, cohort: Any, action: ActionRequest) -> str:
        if self.fail:
            raise RuntimeError("slack is down")
        if self.answer:
            resume(
                self.stack.waits,
                ResumeEvent(run.run_id, wait.wait_id, wait.state_snapshot, {"approved_by": "ana"}),
            )
        self.asked.append(wait.wait_id)
        return self.inner.ask(run=run, wait=wait, cohort=cohort, action=action)


def ask(stack: Stack, run: Run, wait: Wait, asker: Asking, *, asked: int) -> None:
    # The turn host supplies the registry the wait's action is validated against again.
    activities_for(stack.runs.db, asker=asker, turns=ExperimentTurns(stack)).ask_approval(
        AskIntent(
            run_id=run.run_id,
            wait_id=wait.wait_id,
            experiment_id="exp-7",
            experiment_version=VERSION,
            asked=asked,
        )
    )


def test_a_reask_puts_the_question_again_and_moves_its_deadline(stack: Stack, run: Run) -> None:
    wait = park_approval(stack, run)
    frozen(stack, run)
    asker = Asking(stack)
    before = later(timedelta(0))

    ask(stack, run, wait, asker, asked=1)

    moved = stack.waits.get(wait.wait_id)
    assert asker.asked == [wait.wait_id]
    assert moved is not None and moved.reasks == 1
    assert moved.deadline is not None and moved.deadline >= before + APPROVAL_REASK_AFTER
    assert not moved.satisfied, "re-asking is not answering"
    # The question as put: bound to this wait, sized from the frozen cohort, not memory.
    (put,) = posted(stack)
    assert put.wait_id == wait.wait_id and put.summary == wait.approval_summary
    assert put.estimated_customers == 4 and put.experiment_version == VERSION


def test_the_same_reask_run_twice_counts_once(stack: Stack, run: Run) -> None:
    """At-least-once: a rerun puts the question twice (harmless: a second answer to one
    wait is refused) and records the re-ask once. The next interval counts again."""
    wait = park_approval(stack, run)
    frozen(stack, run)
    asker = Asking(stack)

    ask(stack, run, wait, asker, asked=1)
    ask(stack, run, wait, asker, asked=1)
    once = stack.waits.get(wait.wait_id)
    assert once is not None and once.reasks == 1

    ask(stack, run, wait, asker, asked=2)
    twice = stack.waits.get(wait.wait_id)
    assert twice is not None and twice.reasks == 2, "still unanswered: asked again, not expired"
    # Every put is visible, the rerun's duplicate included: a second copy is harmless,
    # a missing one is the silent expiry.
    assert asker.asked == [wait.wait_id] * 3
    assert len(posted(stack)) == 3


def test_an_answered_approval_is_not_asked_again(stack: Stack, run: Run) -> None:
    wait = park_approval(stack, run)
    frozen(stack, run)
    resume(stack.waits, ResumeEvent(run.run_id, wait.wait_id, "fp-1", {"approved_by": "ana"}))
    asker = Asking(stack)

    ask(stack, run, wait, asker, asked=1)

    assert asker.asked == []
    assert posted(stack) == []
    unmoved = stack.waits.get(wait.wait_id)
    assert unmoved is not None and unmoved.reasks == 0


def test_a_failed_reask_is_loud_and_leaves_the_question_due(stack: Stack, run: Run) -> None:
    """A failed notification that moved the deadline anyway would be a silent expiry.
    It fails the activity instead, which Temporal retries; the record is untouched."""
    wait = park_approval(stack, run)
    frozen(stack, run)

    with pytest.raises(RuntimeError, match="slack is down"):
        ask(stack, run, wait, Asking(stack, fail=True), asked=1)

    unmoved = stack.waits.get(wait.wait_id)
    assert unmoved is not None and unmoved.reasks == 0 and unmoved.deadline == wait.deadline
    assert not unmoved.satisfied, "still pending: due to be asked again"
    assert posted(stack) == [], "nothing was put, and nothing claims it was"


def test_an_answer_landing_mid_reask_wins(stack: Stack, run: Run) -> None:
    """Answered while the question was being put: the re-ask is not recorded over it."""
    wait = park_approval(stack, run)
    frozen(stack, run)
    asker = Asking(stack, answer=True)

    ask(stack, run, wait, asker, asked=1)

    (satisfied,) = stack.waits.satisfied_for(run.run_id)
    assert satisfied.wait_id == wait.wait_id and satisfied.reasks == 0
    assert asker.asked == [wait.wait_id], "it was put once; the answer beat the record"
    assert stack.waits.pending_for(run.run_id) == ()


def test_a_wait_that_does_not_say_what_it_asks_is_never_put(stack: Stack, run: Run) -> None:
    """An ask is built from the wait's recorded summary. Without one there is nothing a
    person could decide, so none is invented."""
    wait = park_approval(stack, run, summary=None)

    with pytest.raises(ValueError, match="does not say what it asks about"):
        ask(stack, run, wait, Asking(stack), asked=0)

    assert posted(stack) == []


def posted(stack: Stack) -> list[Any]:
    notifier = stack.notifier
    assert isinstance(notifier, RecordingNotifier)
    return [question for _, question in notifier.posted]
