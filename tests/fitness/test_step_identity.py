"""Part 4: a step boundary is only a boundary if it identifies the step.

The key was `execute:{tool_name}`. EchoEngine returns at most one proposal per turn,
which is why nothing caught it. Real models return several tool calls in one turn and
run_turn loops over them: two refunds on two different charges both key to
`execute:issue_refund`. The first completes, the second finds the completed record,
yields the first's receipt, skips the gateway entirely - and the run reports
status="complete" with evidence that looks correct while the customer is owed two
refunds and got one.

The action fingerprint is already computed for exactly this purpose.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.model.contract import ModelAsset, ModelRequest, ModelResponse, ToolCallProposal
from agentstack.runtime.run import Run
from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.catalog import _refund

from .conftest import SCOPES, TENANT, USER, drive_to_completion

CHARGES = ("ch-7", "ch-8")


class TwoRefundsEngine:
    """A model that proposes two tool calls in one turn, which real models do."""

    def __init__(self) -> None:
        self.asset = ModelAsset(name="two-refunds", context_window=8192, max_output_tokens=512)

    def generate(self, request: ModelRequest) -> ModelResponse:
        # Propose only what this run was actually offered - a fake that ignores the
        # exposure filter would make the wrong tests pass.
        if "issue_refund" not in request.exposed_tools:
            return ModelResponse(text="nothing I can do here")
        return ModelResponse(
            text="refunding both charges",
            proposals=tuple(
                ToolCallProposal(
                    tool="issue_refund",
                    arguments={
                        "tenant": TENANT,
                        "customer_id": "c-42",
                        "charge_id": charge,
                        "amount_cents": 1999,
                    },
                )
                for charge in CHARGES
            ),
        )


def _event(run: Run) -> InboundEvent:
    return InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=run.session_id,
        text="refund both charges",
    )


def test_two_effects_in_one_turn_are_two_steps(stack: Stack, run: Run) -> None:
    stack.deps.engine = TwoRefundsEngine()
    result = drive_to_completion(stack, _event(run), run)

    assert result.status == "complete"
    assert len(stack.client.calls) == 2, (
        f"{len(stack.client.calls)} refund(s) committed for two distinct charges; "
        "the second was swallowed by the first's step record"
    )
    assert {resource for resource, _ in stack.client.calls} == {
        f"{TENANT}/customers/c-42/charges/{charge}" for charge in CHARGES
    }
    assert len(set(result.receipts)) == 2, "the run reported one receipt twice"


def test_the_step_name_carries_the_action_not_just_the_tool(stack: Stack, run: Run) -> None:
    stack.deps.engine = TwoRefundsEngine()
    drive_to_completion(stack, _event(run), run)

    names = {record.name for record in stack.steps.records_for(run.run_id)}
    assert len(names) == 2, f"two distinct actions produced {len(names)} step name(s): {names}"
    for charge in CHARGES:
        fingerprint = _refund(
            {
                "tenant": TENANT,
                "customer_id": "c-42",
                "charge_id": charge,
                "amount_cents": 1999,
            }
        ).fingerprint()
        assert any(fingerprint in name for name in names)


def test_a_repeat_of_the_same_action_is_still_one_step(stack: Stack, run: Run) -> None:
    """Distinguishing steps must not stop the boundary doing its actual job."""
    from .conftest import REFUND_MESSAGE, approve_and_resume

    event = InboundEvent(
        channel="test", tenant=TENANT, user_id=USER, session_id=run.session_id, text=REFUND_MESSAGE
    )
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    for _ in range(3):
        handle(stack, event, scopes=SCOPES, run=run)
    assert len(stack.client.calls) == 1


def test_a_completed_step_is_skipped_by_a_process_that_did_not_run_it(
    stack: Stack, run: Run, app_database: Database, checkpointer: Any
) -> None:
    """The reason step records are durable: replay happens in a new process."""
    stack.deps.engine = TwoRefundsEngine()
    drive_to_completion(stack, _event(run), run)
    assert len(stack.client.calls) == 2

    restarted = build_stack(app_database, checkpointer, tenant=TENANT)
    restarted.deps.engine = TwoRefundsEngine()
    result = drive_to_completion(restarted, _event(run), run)

    assert result.status == "complete"
    assert restarted.client.calls == [], "the replay re-committed through a fresh surface"
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
