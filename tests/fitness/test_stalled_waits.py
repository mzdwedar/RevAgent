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

from agentstack.interfaces import operator_cli
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.triggers import parse_trigger
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.runtime.cycles import UNSETTLED_AFTER, CycleStore
from agentstack.runtime.deadlines import ReaskFailed, fire_reasks
from agentstack.runtime.run import Run
from agentstack.runtime.waits import (
    APPROVAL_REASK_AFTER,
    ResumeEvent,
    Wait,
    WaitWithoutDeadline,
    resume,
)
from agentstack.storage.database import Database, IntegrityViolation

from .conftest import SCOPES, TENANT

WEEK = timedelta(days=7)


def later(delta: timedelta) -> datetime:
    return datetime.now(UTC) + delta


def park_trigger(stack: Stack, run: Run, timeout: timedelta = WEEK) -> Wait:
    return stack.waits.park(
        run_id=run.run_id, kind="trigger", state_snapshot="fp-1", timeout=timeout
    )


def park_approval(stack: Stack, run: Run) -> Wait:
    return stack.waits.park(
        run_id=run.run_id,
        kind="human_approval",
        state_snapshot="fp-1",
        action_fingerprint="fp-action-1",
        approval_summary="roll_out_variant_to_percentage — IRREVERSIBLE",
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


# --- an unanswered approval is asked again, never silently expired --------------------


def test_the_reask_timer_fires_on_an_overdue_approval(stack: Stack, run: Run) -> None:
    wait = park_approval(stack, run)
    asked: list[str] = []
    now = later(APPROVAL_REASK_AFTER + timedelta(minutes=1))

    (moved,) = fire_reasks(stack.waits, now=now, ask=lambda w: asked.append(w.wait_id))

    assert asked == [wait.wait_id]
    assert moved.reasks == 1
    assert moved.deadline == now + APPROVAL_REASK_AFTER
    assert not moved.satisfied, "re-asking is not answering"


def test_it_does_not_fire_early_or_twice_in_one_interval(stack: Stack, run: Run) -> None:
    park_approval(stack, run)
    asked: list[str] = []

    def ask(w: Wait) -> None:
        asked.append(w.wait_id)

    fire_reasks(stack.waits, now=later(APPROVAL_REASK_AFTER - timedelta(minutes=1)), ask=ask)
    assert asked == []

    now = later(APPROVAL_REASK_AFTER + timedelta(minutes=1))
    fire_reasks(stack.waits, now=now, ask=ask)
    fire_reasks(stack.waits, now=now, ask=ask)
    assert len(asked) == 1

    fire_reasks(stack.waits, now=now + APPROVAL_REASK_AFTER, ask=ask)
    assert len(asked) == 2, "still unanswered a day later: asked again, not expired"


def test_an_answered_approval_is_not_asked_again(stack: Stack, run: Run) -> None:
    wait = park_approval(stack, run)
    resume(stack.waits, ResumeEvent(run.run_id, wait.wait_id, "fp-1", {"approved_by": "ana"}))
    asked: list[str] = []

    fire_reasks(stack.waits, now=later(2 * WEEK), ask=lambda w: asked.append(w.wait_id))

    assert asked == []


def test_a_failed_reask_is_loud_and_leaves_the_question_due(stack: Stack, run: Run) -> None:
    """A failed notification that moved the deadline anyway would be a silent expiry."""
    failing = park_approval(stack, run)
    working = park_approval(stack, run)
    asked: list[str] = []

    def ask(w: Wait) -> None:
        if w.wait_id == failing.wait_id:
            raise RuntimeError("slack is down")
        asked.append(w.wait_id)

    now = later(APPROVAL_REASK_AFTER + timedelta(minutes=1))
    with pytest.raises(ReaskFailed, match="slack is down") as caught:
        fire_reasks(stack.waits, now=now, ask=ask)

    assert list(caught.value.failures) == [failing.wait_id]
    assert asked == [working.wait_id], "one failure must not stop the others being asked"
    assert [w.wait_id for w in stack.waits.due_for_reask(now=now)] == [failing.wait_id]


def test_an_answer_landing_mid_reask_wins(stack: Stack, run: Run) -> None:
    """Answered between being read as overdue and having its deadline moved."""
    wait = park_approval(stack, run)

    def answer_while_asking(w: Wait) -> None:
        resume(stack.waits, ResumeEvent(run.run_id, w.wait_id, "fp-1", {"approved_by": "ana"}))

    moved = fire_reasks(stack.waits, now=later(2 * WEEK), ask=answer_while_asking)

    assert moved == ()
    (satisfied,) = stack.waits.satisfied_for(run.run_id)
    assert satisfied.wait_id == wait.wait_id and satisfied.reasks == 0
