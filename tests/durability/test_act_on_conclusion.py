"""L5b-3: what the run does with a conclusion, and no more than the policy's word for it.

Promote is the rollout turn again at PROMOTE_PERCENTAGE, and stops at the same approval: a
person's yes bound to that payload, or nothing happens. Stop is a halt, which the policy
lets through without one. Extend is L5b-2's. The readout is a stand-in (see
`test_conclude_timer.py`); what is under test is what the run does next.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agentstack.context.conclusion_store import ConclusionStore, StoredConclusion
from agentstack.interfaces.slack import RecordingNotifier
from agentstack.interfaces.wiring import Stack, build_stack, deliver
from agentstack.runtime.drafting import PROMOTE_PERCENTAGE, ROLLOUT_PERCENTAGE
from agentstack.runtime.temporal.contracts import (
    EVALUATION,
    OUTCOME_WINDOW,
    PROMOTE,
    STOP,
    RunProgress,
    workflow_id,
)
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database
from agentstack.tools.experiments import EVALUATION_STAGE
from tests.durability.test_approval_wait import PAYLOAD
from tests.durability.test_conclude_timer import approve, options
from tests.temporal_support import progress_until, time_skipping, worker_on

pytestmark = pytest.mark.usefixtures("fixture_dataset")

READOUT = {
    "insv_cents": 123400.0, "insv_lower_cents": 10000.0, "insv_upper_cents": 250000.0,
    "offer": "free-month", "treated": 40, "control": 360,
}  # fmt: skip


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


def concluding(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    def fake(*, store: ConclusionStore, tenant: str, experiment_id: str,
             experiment_version: str, endpoint: int, **_: Any) -> StoredConclusion:  # fmt: skip
        return store.record(
            StoredConclusion(
                tenant,
                experiment_id,
                experiment_version,
                endpoint,
                action,
                "stand-in",
                True,
                {},
                {},
            )
        )

    monkeypatch.setattr("agentstack.runtime.temporal.activities.conclude_experiment", fake)


def events(db: Database) -> list[tuple[str, dict[str, Any]]]:
    rows = db.fetch_all("SELECT kind, payload FROM registry_events ORDER BY id")
    return [(str(kind), dict(payload)) for kind, payload in rows]


def run_until_concluded(
    stack: Stack, db: Database, root: Path, *, then: Any, ready: Any
) -> tuple[RunProgress, Any]:
    """Roll out (approved), let the window pass, and hand over once `ready` says the run is
    where the conclusion put it."""

    async def go() -> tuple[RunProgress, Any]:
        async with (
            time_skipping() as env,
            worker_on(env.client, "q", db, **options(stack, db, root)),
        ):
            run_id = await deliver(stack, env.client, PAYLOAD, source="t", task_queue="q")
            handle = env.client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            parked = await progress_until(handle, lambda p: p.awaiting_approval is not None)
            approve(stack, parked)
            await handle.signal(ExperimentWorkflow.answered, parked.awaiting_approval)
            await progress_until(handle, lambda p: bool(p.answered))
            await env.sleep(OUTCOME_WINDOW + timedelta(hours=1))
            where = await progress_until(handle, ready, timeout=30)
            return where, await then(handle, where)

    return asyncio.run(go())


def test_the_names_the_workflow_uses_are_the_records() -> None:
    assert (PROMOTE, STOP) == ("promote", "stop")
    assert EVALUATION == EVALUATION_STAGE
    assert PROMOTE_PERCENTAGE > ROLLOUT_PERCENTAGE


def test_promote_asks_a_person_for_the_promotion_and_commits_only_their_yes(
    stack: Stack, app_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    concluding(monkeypatch, "promote")

    async def second_approval(handle: Any, where: RunProgress) -> RunProgress:
        assert [kind for kind, _ in events(app_database)].count("rollout") == 1, (
            "a promotion conclusion rolls nothing out before anyone says yes"
        )
        approve(stack, where)
        await handle.signal(ExperimentWorkflow.answered, where.awaiting_approval)
        return await progress_until(
            handle, lambda p: len(p.commits) == 2 and p.awaiting_approval is None, timeout=30
        )

    where, done = run_until_concluded(
        stack,
        app_database,
        tmp_path,
        then=second_approval,
        ready=lambda p: p.awaiting_approval is not None and p.asks == 1 and bool(p.conclusions),
    )

    assert where.awaiting_approval not in done.answered[:1]
    assert [c.status for c in done.commits] == ["committed", "committed"]
    rollouts = [p["percentage"] for kind, p in events(app_database) if kind == "rollout"]
    assert rollouts == [ROLLOUT_PERCENTAGE, PROMOTE_PERCENTAGE]
    notifier = stack.notifier
    assert isinstance(notifier, RecordingNotifier)
    assert [ask.percentage for _, ask in notifier.posted] == [
        ROLLOUT_PERCENTAGE,
        PROMOTE_PERCENTAGE,
    ]
    assert len(done.conclusions) == 1, "a promotion is not measured again"


def test_an_unanswered_promotion_commits_nothing(
    stack: Stack, app_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    concluding(monkeypatch, "promote")

    async def still_asking(handle: Any, _: RunProgress) -> RunProgress:
        return await progress_until(handle, lambda p: p.awaiting_approval is not None, timeout=30)

    _, later = run_until_concluded(
        stack,
        app_database,
        tmp_path,
        then=still_asking,
        ready=lambda p: p.awaiting_approval is not None and bool(p.conclusions),
    )

    assert later.awaiting_approval is not None
    assert [p["percentage"] for k, p in events(app_database) if k == "rollout"] == [
        ROLLOUT_PERCENTAGE
    ]


def test_stop_halts_without_asking_anyone(
    stack: Stack, app_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    concluding(monkeypatch, "stop")

    async def nothing_more(_: Any, where: RunProgress) -> RunProgress:
        return where

    done, _ = run_until_concluded(
        stack,
        app_database,
        tmp_path,
        then=nothing_more,
        ready=lambda p: bool(p.conclusions) and len(p.turns) == 3,
    )

    kinds = [kind for kind, _ in events(app_database)]
    assert kinds.count("halt") == 1 and kinds.count("rollout") == 1
    assert done.awaiting_approval is None, "a halt is the safe direction and waits on no one"
    notifier = stack.notifier
    assert isinstance(notifier, RecordingNotifier)
    assert len(notifier.posted) == 1, "only the first rollout was ever put to a person"


def test_the_channel_is_told_what_was_concluded_and_that_it_was_simulated(
    stack: Stack, app_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    concluding(monkeypatch, "stop")

    async def nothing_more(_: Any, where: RunProgress) -> RunProgress:
        return where

    run_until_concluded(
        stack,
        app_database,
        tmp_path,
        then=nothing_more,
        ready=lambda p: bool(p.conclusions) and len(p.turns) == 3,
    )

    notifier = stack.notifier
    assert isinstance(notifier, RecordingNotifier)
    ((_, notice),) = notifier.conclusions
    first_line = notice.text.splitlines()[0]
    assert "SIMULATED" in first_line and "STOP" in first_line
    assert "model-written; it decided nothing" in notice.text
    assert kinds_of(app_database).count("halt") == 1, "what the model said changed nothing"


def kinds_of(db: Database) -> list[str]:
    return [kind for kind, _ in events(db)]
