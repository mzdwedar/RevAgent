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

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, handle
from agentstack.model.contract import ModelAsset, ModelRequest, ModelResponse, ToolCallProposal
from agentstack.runtime.run import Run
from agentstack.tools.catalog import _refund

from .conftest import SCOPES, TENANT, USER

CHARGES = ("ch-7", "ch-8")


class TwoRefundsEngine:
    """A model that proposes two tool calls in one turn, which real models do."""

    def __init__(self) -> None:
        self.asset = ModelAsset(name="two-refunds", context_window=8192, max_output_tokens=512)

    def generate(self, request: ModelRequest) -> ModelResponse:
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


def _approve_everything(stack: Stack, run: Run, snapshot: str) -> None:
    for charge in CHARGES:
        stack.approvals.grant(
            run_id=run.run_id,
            request=_refund(
                {
                    "tenant": TENANT,
                    "customer_id": "c-42",
                    "charge_id": charge,
                    "amount_cents": 1999,
                }
            ),
            state_snapshot=snapshot,
            approver="finance-oncall",
            summary=f"refund $19.99 on {charge}",
        )


def test_two_effects_in_one_turn_are_two_steps(stack: Stack, run: Run) -> None:
    stack.deps.engine = TwoRefundsEngine()
    event = InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=run.session_id,
        text="refund both charges",
    )

    first = handle(stack, event, scopes=SCOPES, run=run)
    assert first.status == "awaiting_approval"
    assert first.pending_wait is not None
    _approve_everything(stack, run, first.pending_wait.state_snapshot)

    result = handle(stack, event, scopes=SCOPES, run=run)

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
    event = InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=run.session_id,
        text="refund both charges",
    )
    first = handle(stack, event, scopes=SCOPES, run=run)
    assert first.pending_wait is not None
    _approve_everything(stack, run, first.pending_wait.state_snapshot)
    handle(stack, event, scopes=SCOPES, run=run)

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
