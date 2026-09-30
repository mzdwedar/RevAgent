"""Part 4: a step boundary is only a boundary if it identifies the step.

The key was `execute:{tool_name}`. EchoEngine returns at most one proposal per turn,
which is why nothing caught it. Real models return several tool calls in one turn and
run_turn loops over them: two rollouts of two different experiments both key to
`execute:roll_out_variant_to_percentage`. The first completes, the second finds the
completed record, yields the first's receipt, skips the gateway entirely - and the run
reports status="complete" with evidence that looks correct while one experiment never
went out.

The action fingerprint is already computed for exactly this purpose.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.model.contract import ModelAsset, ModelRequest, ModelResponse, ToolCallProposal
from agentstack.policy.approval import ApprovalRequired, ApprovalStale
from agentstack.runtime.run import Run
from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.experiments import prepare_rollout

from .conftest import ROLLOUT_ARGS, SCOPES, TENANT, USER, drive_to_completion, seed_experiment

EXPERIMENTS = ("exp-7", "exp-8")


class TwoRolloutsEngine:
    """A model that proposes two tool calls in one turn, which real models do."""

    def __init__(self) -> None:
        self.asset = ModelAsset(name="two-rollouts", context_window=8192, max_output_tokens=512)

    def generate(self, request: ModelRequest) -> ModelResponse:
        # Propose only what this run was actually offered - a fake that ignores the
        # exposure filter would make the wrong tests pass.
        if "roll_out_variant_to_percentage" not in request.tool_names:
            return ModelResponse(text="nothing I can do here")
        return ModelResponse(
            text="rolling out both experiments",
            proposals=tuple(
                ToolCallProposal(
                    tool="roll_out_variant_to_percentage",
                    arguments={**ROLLOUT_ARGS, "experiment_id": experiment},
                )
                for experiment in EXPERIMENTS
            ),
        )


def _event(run: Run) -> InboundEvent:
    return InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=run.session_id,
        text="roll out both experiments",
    )


def _draft_both(stack: Stack) -> None:
    for experiment in EXPERIMENTS:
        seed_experiment(stack, experiment)


def test_two_effects_in_one_turn_are_two_steps(stack: Stack, run: Run) -> None:
    _draft_both(stack)
    stack.deps.engine = TwoRolloutsEngine()
    result = drive_to_completion(stack, _event(run), run)

    assert result.status == "complete"
    commits = stack.registry_client.rollouts
    assert len(commits) == 2, (
        f"{len(commits)} rollout(s) committed for two distinct experiments; "
        "the second was swallowed by the first's step record"
    )
    assert {resource for resource, _ in commits} == {
        f"{TENANT}/experiments/{experiment}/rollout" for experiment in EXPERIMENTS
    }
    assert len(set(result.receipts)) == 2, "the run reported one receipt twice"


def test_the_step_name_carries_the_action_not_just_the_tool(stack: Stack, run: Run) -> None:
    _draft_both(stack)
    stack.deps.engine = TwoRolloutsEngine()
    drive_to_completion(stack, _event(run), run)

    names = {record.name for record in stack.steps.records_for(run.run_id)}
    assert len(names) == 2, f"two distinct actions produced {len(names)} step name(s): {names}"
    for experiment in EXPERIMENTS:
        fingerprint = prepare_rollout({**ROLLOUT_ARGS, "experiment_id": experiment}).fingerprint()
        assert any(fingerprint in name for name in names)


def test_a_repeat_of_the_same_action_is_still_one_step(stack: Stack, run: Run) -> None:
    """Distinguishing steps must not stop the boundary doing its actual job."""
    from .conftest import ROLLOUT_MESSAGE, approve_and_resume

    seed_experiment(stack)
    event = InboundEvent(
        channel="test", tenant=TENANT, user_id=USER, session_id=run.session_id, text=ROLLOUT_MESSAGE
    )
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    for _ in range(3):
        handle(stack, event, scopes=SCOPES, run=run)
    assert len(stack.registry_client.rollouts) == 1


def test_a_completed_step_is_skipped_by_a_process_that_did_not_run_it(
    stack: Stack, run: Run, app_database: Database, checkpointer: Any
) -> None:
    """The reason step records are durable: replay happens in a new process."""
    _draft_both(stack)
    stack.deps.engine = TwoRolloutsEngine()
    drive_to_completion(stack, _event(run), run)
    assert len(stack.registry_client.rollouts) == 2

    restarted = build_stack(app_database, checkpointer, tenant=TENANT)
    restarted.deps.engine = TwoRolloutsEngine()
    result = drive_to_completion(restarted, _event(run), run)

    assert result.status == "complete"
    assert len(restarted.registry_client.rollouts) == 2, "the replay re-committed"
    assert len(restarted.steps.records_for(run.run_id)) == len(stack.steps.records_for(run.run_id))


def test_the_database_refuses_a_second_completion_of_one_step(
    stack: Stack, run: Run, app_database: Database
) -> None:
    """The ledger checks before writing. The index is what holds when two checks race."""
    with stack.steps.step(run.run_id, "execute:once") as slot:
        slot[0] = "receipt-1"

    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(
            "INSERT INTO run_steps (run_id, name, status, receipt)"
            " VALUES (%s, 'execute:once', 'completed', 'receipt-2')",
            (run.run_id,),
        )

    assert caught.value.constraint == "run_steps_complete_once"


def test_a_step_that_started_and_never_finished_is_reported_as_unknown(
    stack: Stack, run: Run
) -> None:
    """A process died mid-step. The honest answer is not "failed" - it is "nobody knows".

    Calling it failed would invite a clean retry of an effect that may already have
    landed. This is the state the two-phase idempotency ledger exists to settle.
    """
    with (
        pytest.raises(RuntimeError, match="the process died here"),
        stack.steps.step(run.run_id, "execute:interrupted"),
    ):
        raise RuntimeError("the process died here")

    # A raised step records `failed`, which is a known outcome.
    assert stack.steps.started_but_unfinished(run.run_id) == ()

    # A step whose process vanished records nothing after `started`.
    stack.steps._write(run.run_id, "execute:vanished", "started", None)
    unfinished = stack.steps.started_but_unfinished(run.run_id)

    assert [record.name for record in unfinished] == ["execute:vanished"]
    assert stack.steps.completed(run.run_id, "execute:vanished") is None


def test_a_step_stopped_for_approval_is_not_recorded_as_failed(stack: Stack, run: Run) -> None:
    """The pause before an irreversible act is designed, and the ledger says so.

    Recording it as `failed` sent an operator looking for a fault that had not happened,
    and left a real failure indistinguishable from it.
    """
    for name, error in (
        ("execute:paused", ApprovalRequired("no approval for this action")),
        ("execute:stale", ApprovalStale("the world changed since it was granted")),
        ("execute:broken", RuntimeError("the surface fell over")),
    ):
        with (
            pytest.raises(type(error)),
            stack.steps.step(run.run_id, name, pause_on=(ApprovalRequired, ApprovalStale)),
        ):
            raise error

    outcomes = {
        r.name: r.status for r in stack.steps.records_for(run.run_id) if r.status != "started"
    }
    assert outcomes == {
        "execute:paused": "awaiting_approval",
        "execute:stale": "awaiting_approval",
        "execute:broken": "failed",
    }
    # A known outcome, so it is not mistaken for a process that vanished mid-step.
    assert stack.steps.started_but_unfinished(run.run_id) == ()


def test_a_refusal_is_a_pause_only_where_the_caller_parked_a_wait(stack: Stack, run: Run) -> None:
    """`pause_on` is the caller's claim that a wait is parked. Without it the same
    exception is a terminal refusal and the ledger must not say the run is waiting."""
    with pytest.raises(ApprovalRequired), stack.steps.step(run.run_id, "execute:refused"):
        raise ApprovalRequired("a person said no")

    (outcome,) = [r for r in stack.steps.records_for(run.run_id) if r.status != "started"]
    assert outcome.status == "failed"


def test_a_step_for_a_run_that_does_not_exist_is_refused(stack: Stack) -> None:
    with pytest.raises(IntegrityViolation) as caught:
        stack.steps._write("run-never-created", "execute:x", "started", None)

    assert caught.value.constraint == "run_steps_run_id_fkey"


def test_the_run_itself_is_readable_by_a_process_that_did_not_start_it(
    run: Run, app_database: Database, checkpointer: Any
) -> None:
    """Part 4's anchor. A step or a wait keyed on a run nothing recorded is an orphan."""
    restarted = build_stack(app_database, checkpointer, tenant=TENANT)

    found = restarted.runs.get(run.run_id)

    assert found == run
    assert restarted.runs.get("run-never-created") is None
