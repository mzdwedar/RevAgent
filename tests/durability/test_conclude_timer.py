"""L5b-2: the outcome window is a durable timer, and measuring happens once.

After a rollout commits the run sleeps OUTCOME_WINDOW on Temporal's clock, then runs the
conclude activity. The readout itself is L5b-1's (`test_conclude_experiment.py`); here the
measurement is a stand-in that writes through the real store, so what is under test is the
timer and the refusal path. Killing a worker across the window is not covered here.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agentstack.context.conclusion_store import ConclusionStore, StoredConclusion
from agentstack.interfaces.wiring import Stack, build_stack, deliver
from agentstack.policy.approvers import ApprovalReply
from agentstack.runtime.temporal.contracts import OUTCOME_WINDOW, RunProgress, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database, IntegrityViolation
from tests.durability.test_approval_wait import PAYLOAD
from tests.fitness.test_trigger_to_candidate import RULE, StubScorer
from tests.temporal_support import (
    DraftingEngine,
    asker_for,
    progress_until,
    time_skipping,
    turns_for,
    worker_on,
)

pytestmark = pytest.mark.usefixtures("fixture_dataset")

ACTIONS = ("extend", "promote")  # what endpoint 0 and endpoint 1 conclude


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


@pytest.fixture
def measured(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the readout with a stand-in that records through the real store, the way
    `conclude_experiment` does. Returns the endpoints it was asked to measure."""
    asked: list[int] = []

    def fake(*, store: ConclusionStore, tenant: str, experiment_id: str,
             experiment_version: str, endpoint: int, **_: Any) -> StoredConclusion:  # fmt: skip
        asked.append(endpoint)
        return store.record(
            StoredConclusion(
                tenant, experiment_id, experiment_version, endpoint,
                ACTIONS[endpoint], "stand-in", True, {}, {},
            )
        )  # fmt: skip

    monkeypatch.setattr("agentstack.runtime.temporal.activities.conclude_experiment", fake)
    return asked


def options(stack: Stack, db: Database, tmp_path: Path) -> dict[str, Any]:
    return dict(
        scorer=StubScorer(), rule=RULE, turns=turns_for(stack, DraftingEngine()),
        asker=asker_for(stack), conclusions=ConclusionStore(db), outcomes_root=tmp_path,
    )  # fmt: skip


def approve(stack: Stack, parked: RunProgress) -> None:
    with contextlib.suppress(IntegrityViolation):  # the second approval of the same run
        stack.approver_directory.add(
            tenant="acme", slack_user_id="UANA", principal="ana@acme", added_by="l5b2"
        )
    cycle = parked.cycles[0]
    stack.coordinator.apply(
        ApprovalReply(
            run_id=parked.run_id, wait_id=parked.awaiting_approval or "",
            experiment_version=cycle.experiment_version or "", data_as_of=cycle.data_as_of,
            approved=True, slack_user_id="UANA", channel="C1",
        )
    )  # fmt: skip


def conclusions(db: Database) -> list[tuple[int, str]]:
    rows = db.fetch_all("SELECT endpoint, action FROM conclusions ORDER BY endpoint")
    return [(int(r[0]), str(r[1])) for r in rows]


def test_the_window_is_a_timer_and_extend_waits_it_again(
    stack: Stack, app_database: Database, tmp_path: Path, measured: list[int]
) -> None:
    async def go() -> RunProgress:
        async with (
            time_skipping() as env,
            worker_on(env.client, "q", app_database, **options(stack, app_database, tmp_path)),
        ):
            run_id = await deliver(stack, env.client, PAYLOAD, source="t", task_queue="q")
            handle = env.client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            parked = await progress_until(handle, lambda p: p.awaiting_approval is not None)
            approve(stack, parked)
            await handle.signal(ExperimentWorkflow.answered, parked.awaiting_approval)
            await progress_until(handle, lambda p: bool(p.answered))
            assert measured == [], "nothing is measured before the window has passed"
            await env.sleep(OUTCOME_WINDOW + timedelta(hours=1))
            await progress_until(handle, lambda p: len(p.conclusions) == 1, timeout=30)
            assert measured == [0], "extend sleeps the window again before looking again"
            await env.sleep(OUTCOME_WINDOW + timedelta(hours=1))
            return await progress_until(handle, lambda p: len(p.conclusions) == 2, timeout=30)

    done = asyncio.run(go())

    assert [c.action for c in done.conclusions] == ["extend", "promote"]
    assert [c.endpoint for c in done.conclusions] == [0, 1]
    assert measured == [0, 1]
    assert conclusions(app_database) == [(0, "extend"), (1, "promote")]


def test_a_refusal_is_recorded_unread_and_the_run_goes_on(
    stack: Stack, app_database: Database, tmp_path: Path
) -> None:
    """No outcomes file: the activity refuses non-retryably, the run records `unreadable`
    with the refusal's type and does not extend, retry or fail."""

    async def go() -> RunProgress:
        async with (
            time_skipping() as env,
            worker_on(env.client, "q", app_database, **options(stack, app_database, tmp_path)),
        ):
            run_id = await deliver(stack, env.client, PAYLOAD, source="t", task_queue="q")
            handle = env.client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            parked = await progress_until(handle, lambda p: p.awaiting_approval is not None)
            approve(stack, parked)
            await handle.signal(ExperimentWorkflow.answered, parked.awaiting_approval)
            await progress_until(handle, lambda p: bool(p.answered))
            await env.sleep(OUTCOME_WINDOW + timedelta(hours=1))
            return await progress_until(handle, lambda p: len(p.conclusions) == 1, timeout=30)

    (only,) = asyncio.run(go()).conclusions

    assert dataclasses.replace(only, refusal=None).status == "unreadable"
    assert only.refusal in {"FileNotFoundError", "ConclusionRefused"}
    assert conclusions(app_database) == []
