"""A1 (C1): what a person is asked about is what commits.

The rollout turn is told the frozen cohort's rollout, argument for argument, and the
model's proposal is what an approval binds and what commits (T43). Nothing used to hold
the proposal to what it was told. A model that proposed `percentage=100`, or a looser
`risk_threshold`, was headlined to the approver as "~4 customers (10%)", from a constant,
and a "yes" would have rolled out what the model said.

Now a proposal that differs from the frozen cohort is refused in the turn, before it is
parked, so nobody is ever asked about it; the refusal is audited. The ask and the act
each hold the wait's action to the cohort again. And the headline is computed from the
action the wait holds: the payload that commits if the answer is yes.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from agentstack.context.datasets import REGISTRY
from agentstack.context.frozen_cohorts import FrozenCohort, FrozenCohortStore
from agentstack.interfaces.slack import RecordingNotifier, render_blocks
from agentstack.interfaces.wiring import ChannelAsker, Stack, answer, deliver
from agentstack.model.contract import ModelRequest, ModelResponse, ToolCallProposal
from agentstack.runtime.drafting import PROPOSAL_DEVIATES, estimated_customers, intended_rollout
from agentstack.runtime.run import new_run
from agentstack.runtime.temporal.activities import DEVIATES
from agentstack.runtime.temporal.contracts import AskIntent, RunProgress, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.runtime.waits import ResumeEvent, Wait, WaitStore, resume
from agentstack.storage.database import Database
from agentstack.tools.experiments import ROLLOUT, prepare_rollout
from tests.durability.test_approval_wait import PAYLOAD, posted, propose_and_wait
from tests.durability.test_approval_wait import stack as stack  # the same fixture
from tests.durability.test_commit import acted, commit_again, held
from tests.durability.test_slack_answer import APPROVER, slack_click
from tests.fitness.test_trigger_to_candidate import RULE, StubScorer
from tests.temporal_support import (
    DraftingEngine,
    activities_for,
    asker_for,
    progress_until,
    time_skipping,
    turns_for,
    worker_on,
)

pytestmark = pytest.mark.usefixtures("fixture_dataset")


@pytest.fixture(autouse=True)
def _approver(stack: Stack) -> None:
    stack.approver_directory.add(
        tenant="acme", slack_user_id=APPROVER, principal="ana@acme", added_by="a1"
    )


class DeviatingEngine(DraftingEngine):
    """Drafts as told, then proposes a rollout that is not the one it was told: what an
    injected instruction, or a model that simply got it wrong, would do."""

    def __init__(self, changes: dict[str, Any]) -> None:
        super().__init__()
        self.changes = changes

    def _answer(self, request: ModelRequest) -> ModelResponse:
        told = super()._answer(request)
        return ModelResponse(
            text=told.text,
            proposals=tuple(
                ToolCallProposal(tool=p.tool, arguments={**p.arguments, **self.changes})
                if p.tool == ROLLOUT.name
                else p
                for p in told.proposals
            ),
        )


DEVIATIONS = {
    "everyone": {"percentage": 100},
    "a-looser-threshold": {"risk_threshold": 0.05},
    "another-cohort": {"experiment_version": "exp:not-the-frozen-one"},
    "another-model": {"targeting_model_version": "some-other-model"},
}


@pytest.mark.parametrize("changes", DEVIATIONS.values(), ids=DEVIATIONS.keys())
def test_a_proposal_that_differs_from_the_frozen_cohort_is_never_put_to_a_person(
    stack: Stack, app_database: Database, changes: dict[str, Any]
) -> None:
    async def go() -> tuple[str, RunProgress]:
        async with (
            time_skipping() as env,
            worker_on(
                env.client,
                "admission",
                app_database,
                scorer=StubScorer(),
                rule=RULE,
                turns=turns_for(stack, DeviatingEngine(changes)),
                asker=asker_for(stack),
            ),
        ):
            run_id = await deliver(stack, env.client, PAYLOAD, source="a1", task_queue="admission")
            handle = env.client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            # The rollout turn has ended and the run is back waiting for data.
            done = await progress_until(
                handle, lambda p: len(p.turns) == 2 and p.waiting_on is not None
            )
            return run_id, done

    run_id, done = asyncio.run(go())

    draft, rollout = done.turns
    assert draft.receipts == 1, "the draft is the model's to write, and was written"
    assert (rollout.status, rollout.refusals, rollout.wait_id) == ("rejected", 1, None)
    assert done.awaiting_approval is None and done.asks == 0 and done.commits == ()
    assert posted(stack) == [], "nobody was asked about it"
    parked = app_database.fetch_all(
        "SELECT wait_id FROM waits WHERE run_id = %s AND kind = 'human_approval'", (run_id,)
    )
    assert parked == [], "nothing was parked that a person could be asked about later"
    assert stack.registry_client.rollouts == []
    # Refused, and on the record: under the agent that proposed it, before any gateway.
    (refused,) = [r for r in stack.audit.for_run(run_id) if r.policy_decision == PROPOSAL_DEVIATES]
    assert refused.outcome == "refused" and refused.resource.endswith("/rollout")
    assert refused.principal == "agent-operator"
    # The gateway never saw the rollout: its only rollout record is the refusal above.
    rollout = [r for r in stack.audit.for_run(run_id) if r.resource.endswith("/rollout")]
    assert rollout == [refused]


def test_the_headline_is_the_payload_that_commits(stack: Stack, app_database: Database) -> None:
    """The percentage, headcount and version the person reads are the ones that roll out."""

    async def approve(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await answer(stack, env.client, **slack_click(parked))
        return await acted(handle)

    _, parked, done = propose_and_wait(stack, app_database, then=approve)

    assert done.commits[0].status == "committed"
    (asked,) = posted(stack)
    ((_, payload),) = stack.registry_client.rollouts
    frozen = FrozenCohortStore(db=app_database).get(
        tenant="acme",
        experiment_id="exp-7",
        experiment_version=parked.cycles[0].experiment_version or "",
    )
    assert frozen is not None
    assert asked.percentage == payload["percentage"]
    assert asked.estimated_customers == estimated_customers(frozen, payload["percentage"])
    assert asked.experiment_version == payload["experiment_version"]


def test_the_question_is_sized_from_the_action_it_binds_not_from_a_constant() -> None:
    """The asker is handed the action the wait holds, and every rollout number it states
    is that action's. Held here with an action whose percentage is not the default."""
    notifier = RecordingNotifier()
    cohort = FrozenCohort(
        tenant="acme",
        experiment_id="exp-7",
        experiment_version="exp:v1",
        data_as_of="fixture:abc",
        targeting_model_version="stub-1",
        risk_threshold=0.6,
        size=480,
        annual_value_at_risk_cents=1_000_000,
        description={},
    )
    action = prepare_rollout(intended_rollout(cohort, prior_rollout_event=0) | {"percentage": 25})
    run = new_run(session_id="s", tenant="acme", user="agent-operator", channel="t")
    wait = Wait(
        wait_id="wait-1",
        run_id=run.run_id,
        kind="human_approval",
        state_snapshot="s",
        created_at=datetime.now(UTC),
        action_fingerprint=action.fingerprint(),
        approval_summary="roll_out_variant_to_percentage — IRREVERSIBLE",
    )

    ChannelAsker(notifier).ask(run=run, wait=wait, cohort=cohort, action=action)

    ((_, asked),) = notifier.posted
    assert (asked.percentage, asked.estimated_customers) == (25, 120)
    assert asked.experiment_version == "exp:v1"
    assert asked.currency == "USD"


def test_the_question_carries_the_frozen_cohorts_currency() -> None:
    """The figure the approver weighs is in the cohort's currency, read from the record
    the cohort was frozen as, not assumed."""
    notifier = RecordingNotifier()
    cohort = FrozenCohort(
        tenant="acme",
        experiment_id="exp-7",
        experiment_version="exp:v1",
        data_as_of="kkbox-churn:abc",
        targeting_model_version="stub-1",
        risk_threshold=0.6,
        size=480,
        annual_value_at_risk_cents=1_000_000,
        description={"currency": "NTD"},
    )
    action = prepare_rollout(intended_rollout(cohort, prior_rollout_event=0))
    run = new_run(session_id="s", tenant="acme", user="agent-operator", channel="t")
    wait = Wait(
        wait_id="wait-1",
        run_id=run.run_id,
        kind="human_approval",
        state_snapshot="s",
        created_at=datetime.now(UTC),
        action_fingerprint=action.fingerprint(),
        approval_summary="roll_out_variant_to_percentage — IRREVERSIBLE",
    )

    ChannelAsker(notifier).ask(run=run, wait=wait, cohort=cohort, action=action)

    ((_, asked),) = notifier.posted
    assert asked.money() == "NTD 10,000"


def test_a_non_usd_cohort_goes_from_trigger_to_a_bound_approval_and_one_rollout(
    stack: Stack, app_database: Database
) -> None:
    """K15, the CI variant: the KKBox cohort's shape (NTD, revenue observed, no hosted
    scorer) on a tiny fixture and a deterministic scorer. The question names a headcount
    and an NTD figure, the rollout waits for the answer, and the approval is bound to the
    run, the fingerprint and the snapshot the wait recorded. The local variant is the
    worker with `RecordedScorers` on the real recording."""
    REGISTRY["fixture"] = replace(REGISTRY["fixture"], currency="NTD")

    async def approve(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        assert stack.registry_client.rollouts == [], "the rollout went out before anyone answered"
        await answer(stack, env.client, **slack_click(parked))
        return await acted(handle)

    run_id, parked, done = propose_and_wait(stack, app_database, then=approve)

    (asked,) = posted(stack)
    assert asked.currency == "NTD" and asked.money() == "NTD 24,000"
    assert asked.estimated_customers == 4
    rendered = str(render_blocks(asked))
    assert "Roll out to ~4 customers" in rendered and "NTD 24,000" in rendered
    assert "$" not in rendered

    wait = WaitStore(db=app_database).get(parked.awaiting_approval or "")
    assert wait is not None and wait.action_fingerprint
    approval = stack.approvals.find(
        run_id=run_id,
        action_fingerprint=wait.action_fingerprint,
        state_snapshot=wait.state_snapshot,
    )
    assert approval is not None and approval.by_a_human is True
    assert done.commits[0].status == "committed"
    assert len(stack.registry_client.rollouts) == 1


def park_deviating(stack: Stack, parked: RunProgress, **changes: Any) -> Wait:
    """A wait holding a rollout the frozen cohort doesn't get, parked by something other
    than the turn: the ask and the act have to refuse it on their own."""
    arguments = held(parked, stack, **changes)
    return stack.waits.park(
        run_id=parked.run_id,
        kind="human_approval",
        state_snapshot="s",
        action_fingerprint=prepare_rollout(arguments).fingerprint(),
        approval_summary="roll_out_variant_to_percentage — IRREVERSIBLE",
        action_tool=ROLLOUT.name,
        action_arguments=arguments,
    )


def test_a_wait_holding_another_rollout_is_never_asked_about(
    stack: Stack, app_database: Database
) -> None:
    _, parked, _ = propose_and_wait(stack, app_database)
    wait = park_deviating(stack, parked, percentage=100)
    activities = activities_for(
        app_database, turns=turns_for(stack, DraftingEngine()), asker=asker_for(stack)
    )

    with pytest.raises(ApplicationError) as refused:
        ActivityEnvironment().run(
            activities.ask_approval,
            AskIntent(
                run_id=parked.run_id,
                wait_id=wait.wait_id,
                experiment_id="exp-7",
                experiment_version=parked.cycles[0].experiment_version or "",
                asked=0,
            ),
        )

    assert refused.value.type == DEVIATES and refused.value.non_retryable
    assert [a.wait_id for a in posted(stack)] == [parked.awaiting_approval], "only the real one"
    (record,) = [r for r in stack.audit.for_run(parked.run_id) if r.wait_id == wait.wait_id]
    assert (record.policy_decision, record.outcome) == (PROPOSAL_DEVIATES, "refused")


def test_a_wait_holding_another_rollout_is_never_committed_even_approved(
    stack: Stack, app_database: Database
) -> None:
    _, parked, _ = propose_and_wait(stack, app_database)
    wait = park_deviating(stack, parked, risk_threshold=0.05)
    resume(stack.waits, ResumeEvent(parked.run_id, wait.wait_id, "s", {"approved_by": "ana"}))
    stack.approvals.grant_for_fingerprint(
        run_id=parked.run_id,
        action_fingerprint=wait.action_fingerprint or "",
        state_snapshot="s",
        approver="ana@acme",
        summary="roll_out_variant_to_percentage — IRREVERSIBLE",
    )

    with pytest.raises(ApplicationError) as refused:
        commit_again(stack, app_database, parked, wait_id=wait.wait_id)

    assert refused.value.type == DEVIATES and refused.value.non_retryable
    assert stack.registry_client.rollouts == []
    (record,) = [r for r in stack.audit.for_run(parked.run_id) if r.wait_id == wait.wait_id]
    assert (record.policy_decision, record.outcome) == (PROPOSAL_DEVIATES, "refused")
