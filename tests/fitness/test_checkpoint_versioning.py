"""Criterion 21: an incompatible checkpoint parks the run. And criterion 2's loud half.

A run that waited across a deploy resumes, or fails loudly with a migration message -
never silently diverges. The divergence is the dangerous case because nothing breaks:
new code reads an old checkpoint, a renamed key comes back empty, and the turn carries
on from a state it only thinks it recognises.

The "deploy" here is the one thing a deploy changes that matters: which schema versions
the running code will accept.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentstack.interfaces import operator_cli
from agentstack.interfaces.wiring import Stack, build_stack, envelope_for
from agentstack.runtime import graph
from agentstack.runtime.graph import NeedsMigration, turn_thread
from agentstack.runtime.loop import run_turn
from agentstack.runtime.run import Run
from agentstack.runtime.waits import NEEDS_MIGRATION, ResumeEvent, resume
from agentstack.storage.database import Database
from agentstack.tools.spec import Surface

from .conftest import SCOPES, TENANT
from .test_turn_graph import CountingEngine, RefusingClient

MESSAGE = "lookup_subscription tenant=acme customer_id=c-42"


def envelope(stack: Stack, run: Run) -> Any:
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    return envelope_for(view, scopes=SCOPES)


def died_mid_turn(stack: Stack, run: Run, turn_id: str = "t1") -> tuple[CountingEngine, Any]:
    """A turn that answered, then died before acting: a checkpoint with work left."""
    engine = CountingEngine(stack.deps.engine)
    stack.deps.engine = engine
    surface = RefusingClient(stack.client)
    stack.deps.gateway.surfaces[Surface.API] = surface
    with pytest.raises(RuntimeError, match="the process died here"):
        run_turn(
            run=run,
            envelope=envelope(stack, run),
            message=MESSAGE,
            deps=stack.deps,
            turn_id=turn_id,
        )
    surface.reachable = True
    return engine, surface


def deploy_incompatible_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph, "CHECKPOINT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(graph, "COMPATIBLE_SCHEMA_VERSIONS", frozenset({2}))


def resume_turn(stack: Stack, run: Run, turn_id: str = "t1") -> Any:
    return run_turn(
        run=run, envelope=envelope(stack, run), message=MESSAGE, deps=stack.deps, turn_id=turn_id
    )


# --- every checkpoint says what shape it is ---------------------------------------------


def test_every_checkpoint_a_turn_writes_carries_the_schema_version(stack: Stack, run: Run) -> None:
    resume_turn(stack, run, turn_id="versioned")

    history = list(stack.deps.graph.get_state_history(turn_thread(run.run_id, "versioned")))

    assert len(history) > 3, "expected a checkpoint per node"
    # LangGraph's first checkpoint is written before the input is applied and holds no
    # state at all. Every one that holds state says what shape it is.
    unversioned = [h for h in history if "schema_version" not in h.values]
    assert all(h.values == {} for h in unversioned)
    assert {h.values["schema_version"] for h in history if h.values} == {
        graph.CHECKPOINT_SCHEMA_VERSION
    }


def test_the_current_version_is_one_this_code_can_resume() -> None:
    assert graph.CHECKPOINT_SCHEMA_VERSION in graph.COMPATIBLE_SCHEMA_VERSIONS


def test_a_compatible_checkpoint_still_resumes(stack: Stack, run: Run) -> None:
    engine, _ = died_mid_turn(stack, run)

    result = resume_turn(stack, run)

    assert result.status == "complete"
    assert engine.calls == 1
    assert stack.waits.pending_for(run.run_id) == ()


# --- an incompatible one parks the run and never resumes into it ------------------------


def test_an_incompatible_checkpoint_parks_the_run_and_fails_loudly(
    stack: Stack, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = died_mid_turn(stack, run)
    deploy_incompatible_change(monkeypatch)

    with pytest.raises(NeedsMigration, match=r"schema v1; this code resumes v2"):
        resume_turn(stack, run)

    (parked,) = stack.waits.pending_for(run.run_id)
    assert parked.kind == NEEDS_MIGRATION
    assert parked.state_snapshot.startswith(f"{run.run_id}:t1@")
    assert parked.state_snapshot.endswith("schema v1")
    assert engine.calls == 1, "the model was asked again from a state nobody could read"
    assert stack.client.calls == [], "an effect was committed from a misread checkpoint"


def test_a_checkpoint_that_does_not_say_its_version_is_not_guessed(stack: Stack, run: Run) -> None:
    """Checkpoints written before T19 carry no version. Assuming "probably v1" is the
    exact confidence this criterion exists to remove."""
    died_mid_turn(stack, run)
    config = turn_thread(run.run_id, "t1")
    stack.deps.graph.update_state(config, {"schema_version": None})

    with pytest.raises(NeedsMigration, match="schema vNone"):
        resume_turn(stack, run)


def test_a_finished_turn_in_an_old_shape_is_not_reused_either(
    stack: Stack, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invoking a finished thread with new input merges it into the old values."""
    resume_turn(stack, run, turn_id="done")
    deploy_incompatible_change(monkeypatch)

    with pytest.raises(NeedsMigration):
        resume_turn(stack, run, turn_id="done")


def test_the_parked_run_takes_no_other_turn_either(
    stack: Stack, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parked means the run, not one thread of it: a fresh turn is blocked by the wait."""
    died_mid_turn(stack, run)
    deploy_incompatible_change(monkeypatch)
    with pytest.raises(NeedsMigration):
        resume_turn(stack, run)

    fresh = resume_turn(stack, run, turn_id="a-new-turn")

    assert fresh.status == "blocked"
    assert NEEDS_MIGRATION in fresh.text
    assert stack.client.calls == []


def test_asking_again_does_not_park_it_twice(
    stack: Stack, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    died_mid_turn(stack, run)
    deploy_incompatible_change(monkeypatch)
    for _ in range(3):
        with pytest.raises(NeedsMigration):
            resume_turn(stack, run)

    assert len(stack.waits.pending_for(run.run_id)) == 1


def test_once_migrated_and_released_the_run_resumes(
    stack: Stack, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remedy: rewrite the checkpoint into the new shape, then satisfy the wait
    against the checkpoint it was parked on. Still without asking the model again."""
    engine, _ = died_mid_turn(stack, run)
    deploy_incompatible_change(monkeypatch)
    with pytest.raises(NeedsMigration):
        resume_turn(stack, run)
    (parked,) = stack.waits.pending_for(run.run_id)

    stack.deps.graph.update_state(turn_thread(run.run_id, "t1"), {"schema_version": 2})
    resume(
        stack.waits,
        ResumeEvent(run.run_id, parked.wait_id, parked.state_snapshot, {"migrated_by": "ops"}),
    )
    result = resume_turn(stack, run)

    assert result.status == "complete"
    assert engine.calls == 1


def test_parking_survives_the_process(
    stack: Stack,
    run: Run,
    monkeypatch: pytest.MonkeyPatch,
    app_database: Database,
    checkpointer: Any,
) -> None:
    died_mid_turn(stack, run)
    deploy_incompatible_change(monkeypatch)
    with pytest.raises(NeedsMigration):
        resume_turn(stack, run)

    restarted = build_stack(app_database, checkpointer, tenant=TENANT)

    assert [w.kind for w in restarted.waits.pending_for(run.run_id)] == [NEEDS_MIGRATION]


# --- reported by `operator stalled`, as itself ------------------------------------------


def test_operator_stalled_reports_it_distinct_from_a_stalled_trigger(
    stack: Stack,
    run: Run,
    monkeypatch: pytest.MonkeyPatch,
    app_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    died_mid_turn(stack, run)
    deploy_incompatible_change(monkeypatch)
    with pytest.raises(NeedsMigration):
        resume_turn(stack, run)
    stack.waits.park(
        run_id=run.run_id, kind="trigger", state_snapshot="fp-1", timeout=timedelta(hours=1)
    )

    code = operator_cli.main(
        ["--url", app_database_url, "stalled"], now=datetime.now(UTC) + timedelta(days=1)
    )

    out = capsys.readouterr().out
    assert code == 1
    lines = [line.strip() for line in out.splitlines()]
    assert sum(line.startswith(f"{NEEDS_MIGRATION}  wait-") for line in lines) == 1
    assert sum(line.startswith("stalled  wait-") for line in lines) == 1
    assert "schema v1" in out
    assert f"1 stalled, 1 {NEEDS_MIGRATION}" in out


def test_needs_migration_is_reported_however_recent(
    stack: Stack, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It has no deadline to pass: it was never going to resume by waiting."""
    died_mid_turn(stack, run)
    deploy_incompatible_change(monkeypatch)
    with pytest.raises(NeedsMigration):
        resume_turn(stack, run)

    (reported,) = stack.waits.stalled(now=datetime.now(UTC))
    assert reported.kind == NEEDS_MIGRATION
    assert stack.waits.stalled(now=datetime.now(UTC), older_than=timedelta(days=1)) == ()
