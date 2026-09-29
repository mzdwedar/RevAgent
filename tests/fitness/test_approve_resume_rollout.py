"""Criterion 23's mechanism: a human's answer becomes an approval the gateway accepts.

The process that parked the wait held the prepared request and the rendered prompt in
memory. The process handling the click has neither, so the wait records what it is
asking about and this reads it back. The durability half - killing the process while
parked - is `tests/durability/test_approve_after_death.py`.

Nothing here commits the rollout. The next turn does, through the gateway, which is
what keeps the commit on the one path that has idempotency, containment and an audit
record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentstack.execution.gateway import Gateway
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, envelope_for, handle
from agentstack.model.contract import ModelAsset, ModelRequest, ModelResponse, ToolCallProposal
from agentstack.observability.spans import Tracer
from agentstack.policy.approval import ApprovalRequired
from agentstack.policy.approvers import ApprovalReply, ApproverNotAuthorized
from agentstack.runtime.approvals import ReplyNotApplicable
from agentstack.runtime.loop import TurnResult
from agentstack.runtime.run import Run, new_run
from agentstack.runtime.waits import Wait
from agentstack.tools.experiments import HISTORY, ROLLOUT, ROLLOUT_STAGE, prepare_rollout

from .conftest import TENANT, USER

SCOPES = frozenset({"experiments:draft", "experiments:rollout"})
ROLLOUT_ARGS = {
    "tenant": TENANT,
    "experiment_id": "exp-7",
    "experiment_version": "exp:5cbf2762",
    "percentage": 10,
    "targeting_model_version": "tabpfn-3.5",
    "risk_threshold": 0.61,
    "prior_rollout_event": 0,
}


@pytest.fixture
def parked(stack: Stack) -> tuple[Run, Wait]:
    """A run that reached the rollout and parked on a human."""
    # What an earlier drafting turn wrote. The registry refuses to roll out an
    # experiment that was never drafted, at a version that is not its current one.
    stack.registry_client.commit(
        f"{TENANT}/experiments/{ROLLOUT_ARGS['experiment_id']}",
        {
            "experiment_version": ROLLOUT_ARGS["experiment_version"],
            "hypothesis": "a discount retains at-risk customers",
            "variant": "20-percent-off",
        },
    )
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    run = stack.runs.ensure(
        new_run(
            session_id=session.session_id,
            tenant=TENANT,
            user=USER,
            stage=ROLLOUT_STAGE,
            channel="test",
        )
    )
    request = prepare_rollout(ROLLOUT_ARGS)
    wait = stack.waits.park(
        run_id=run.run_id,
        kind="human_approval",
        state_snapshot="as-shown",
        action_fingerprint=request.fingerprint(),
        approval_summary="roll_out_variant_to_percentage — IRREVERSIBLE",
    )
    stack.approver_directory.add(
        tenant=TENANT, slack_user_id="UANA", principal="ana@acme", added_by="ops"
    )
    return run, wait


def reply(run: Run, wait: Wait, *, user: str = "UANA", approved: bool = True) -> ApprovalReply:
    return ApprovalReply(
        run_id=run.run_id,
        wait_id=wait.wait_id,
        experiment_version="exp:5cbf2762",
        data_as_of="telecom-bigml:f107d488",
        approved=approved,
        slack_user_id=user,
        channel="C1",
    )


def commit_rollout(stack: Stack, run: Run) -> object:
    view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)
    gateway: Gateway = stack.deps.gateway
    return gateway.execute(
        request=prepare_rollout(ROLLOUT_ARGS),
        spec=ROLLOUT,
        envelope=envelope_for(view, scopes=SCOPES),
        run_id=run.run_id,
        state_snapshot="as-shown",
        tracer=tracer,
    )


# --- the path ---


def test_an_approved_reply_lets_the_rollout_commit_exactly_once(
    stack: Stack, parked: tuple[Run, Wait]
) -> None:
    run, wait = parked

    with pytest.raises(ApprovalRequired):
        commit_rollout(stack, run)

    resolution = stack.coordinator.apply(reply(run, wait))
    assert resolution.approved is True
    assert resolution.approver == "ana@acme"

    first = commit_rollout(stack, run)
    second = commit_rollout(stack, run)

    assert len(stack.registry_client.rollouts) == 1, "the variant went out twice"
    assert second.deduplicated is True
    assert first.receipt == second.receipt


def test_the_wait_is_satisfied_and_names_who_answered(
    stack: Stack, parked: tuple[Run, Wait]
) -> None:
    run, wait = parked

    stack.coordinator.apply(reply(run, wait))

    assert stack.waits.pending_for(run.run_id) == ()
    satisfied = stack.waits.satisfied_for(run.run_id)
    assert satisfied[0].payload["approved_by"] == "ana@acme"
    assert satisfied[0].payload["slack_user_id"] == "UANA"


def test_the_approval_is_bound_to_the_fingerprint_the_wait_recorded(
    stack: Stack, parked: tuple[Run, Wait]
) -> None:
    """Not to one recomputed here. Anything recomputed could differ from what the
    approver was actually shown."""
    run, wait = parked

    stack.coordinator.apply(reply(run, wait))

    found = stack.approvals.find(
        run_id=run.run_id,
        action_fingerprint=wait.action_fingerprint or "",
        state_snapshot="as-shown",
    )
    assert found is not None
    assert found.approver == "ana@acme"
    assert found.by_a_human is True
    assert found.summary == "roll_out_variant_to_percentage — IRREVERSIBLE"


def test_a_refusal_satisfies_the_wait_and_grants_nothing(
    stack: Stack, parked: tuple[Run, Wait]
) -> None:
    """ "No" is an answer. The run stops waiting and the rollout still cannot commit."""
    run, wait = parked

    resolution = stack.coordinator.apply(reply(run, wait, approved=False))

    assert resolution.approved is False
    assert resolution.approval_id is None
    assert stack.waits.satisfied_for(run.run_id)[0].payload["refused_by"] == "ana@acme"
    with pytest.raises(ApprovalRequired):
        commit_rollout(stack, run)
    assert stack.registry_client.rollouts == []


# --- the refusals ---


def test_an_outsider_cannot_approve_even_with_a_valid_wait(
    stack: Stack, parked: tuple[Run, Wait]
) -> None:
    """Criterion 25, on the path that matters."""
    run, wait = parked

    with pytest.raises(ApproverNotAuthorized):
        stack.coordinator.apply(reply(run, wait, user="UOUTSIDER"))

    assert stack.waits.pending_for(run.run_id), "a refused approver left the run unparked"


def test_an_approver_for_another_tenant_cannot_approve(
    stack: Stack, parked: tuple[Run, Wait]
) -> None:
    """The tenant comes from the run, so standing elsewhere buys nothing here."""
    run, wait = parked
    stack.approver_directory.add(
        tenant="other", slack_user_id="UBEN", principal="ben@other", added_by="ops"
    )

    with pytest.raises(ApproverNotAuthorized, match=TENANT):
        stack.coordinator.apply(reply(run, wait, user="UBEN"))


def test_an_answer_to_a_run_that_does_not_exist_is_refused(stack: Stack) -> None:
    run = new_run(session_id="s", tenant=TENANT, user=USER, channel="test")
    wait = Wait(
        wait_id="wait-ghost",
        run_id=run.run_id,
        kind="human_approval",
        state_snapshot="s",
        created_at=datetime.now(UTC),
    )

    with pytest.raises(ReplyNotApplicable, match="not a run"):
        stack.coordinator.apply(reply(run, wait))


def test_answering_the_same_question_twice_is_refused(
    stack: Stack, parked: tuple[Run, Wait]
) -> None:
    """The second reply to one question is not a second decision."""
    run, wait = parked
    stack.coordinator.apply(reply(run, wait))

    with pytest.raises(ReplyNotApplicable, match="already answered"):
        stack.coordinator.apply(reply(run, wait))

    assert len(stack.waits.satisfied_for(run.run_id)) == 1, "one question, one answer"


def test_a_wait_belonging_to_another_run_is_refused(stack: Stack, parked: tuple[Run, Wait]) -> None:
    run, wait = parked
    other = stack.runs.ensure(
        new_run(session_id=run.session_id, tenant=TENANT, user=USER, channel="test")
    )

    with pytest.raises(ReplyNotApplicable, match="not a pending wait"):
        stack.coordinator.apply(reply(other, wait))


def test_a_wait_that_does_not_say_what_it_asks_grants_nothing(
    stack: Stack, parked: tuple[Run, Wait]
) -> None:
    """Older waits predate the column. Binding an approval to a fingerprint nobody
    recorded would be approving something unnamed."""
    run, _ = parked
    trigger_wait = stack.waits.park(
        run_id=run.run_id, kind="trigger", state_snapshot="as-shown", timeout=timedelta(days=7)
    )

    with pytest.raises(ReplyNotApplicable, match="does not record what it was asking"):
        stack.coordinator.apply(reply(run, trigger_wait))


def test_the_coordinator_commits_nothing_itself() -> None:
    """The rollout goes through the gateway on the next turn, which is the one path
    with idempotency, containment and an audit record.

    Checked against what the module *imports*, not against its prose: the docstring
    says "gateway" precisely because it is explaining that it does not use one.
    """
    from agentstack.runtime import approvals

    imported = {name for name in vars(approvals) if not name.startswith("_")}

    assert "Gateway" not in imported
    assert not any("surface" in name.lower() for name in imported)
    assert "ApprovalCoordinator" in imported


# --- through the request path: what the session read is not what was approved ---
#
# A read in one turn reaches the next as an observation (STACK.md row 5). That is what
# the session has looked at, not what the human was asked about. Bound into the
# approval's snapshot, it made the approval stale on the next read, and the person was
# asked again - once per read.

READ_SCOPES = frozenset({"experiments:read", "experiments:rollout"})
ROLL_OUT = (
    "roll_out_variant_to_percentage tenant=acme experiment_id=exp-7 "
    "experiment_version=exp:5cbf2762 percentage=10 "
    "targeting_model_version=tabpfn-3.5 risk_threshold=0.61 prior_rollout_event=0"
)
LOOK_AT_HISTORY = "get_rollout_history tenant=acme experiment_id=exp-7"


class ReadThenRollOutEngine:
    """A model that looks before it acts, in one turn - which is what a real one does."""

    def __init__(self) -> None:
        self.asset = ModelAsset(name="read-then-roll", context_window=8192, max_output_tokens=512)

    def generate(self, request: ModelRequest) -> ModelResponse:
        # Only what this run was offered: a fake that ignores exposure proves nothing.
        if not {HISTORY.name, ROLLOUT.name} <= set(request.tool_names):
            return ModelResponse(text="nothing I can do here")
        return ModelResponse(
            text="checked the history, rolling out",
            proposals=(
                ToolCallProposal(
                    tool=HISTORY.name,
                    arguments={"tenant": TENANT, "experiment_id": "exp-7"},
                ),
                ToolCallProposal(tool=ROLLOUT.name, arguments=dict(ROLLOUT_ARGS)),
            ),
        )


@pytest.fixture
def rollout_run(stack: Stack) -> Run:
    """A drafted experiment, a rollout-stage run, and someone allowed to approve it."""
    stack.registry_client.commit(
        f"{TENANT}/experiments/{ROLLOUT_ARGS['experiment_id']}",
        {
            "experiment_version": ROLLOUT_ARGS["experiment_version"],
            "hypothesis": "a discount retains at-risk customers",
            "variant": "20-percent-off",
        },
    )
    stack.approver_directory.add(
        tenant=TENANT, slack_user_id="UANA", principal="ana@acme", added_by="ops"
    )
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    return stack.runs.ensure(
        new_run(
            session_id=session.session_id,
            tenant=TENANT,
            user=USER,
            stage=ROLLOUT_STAGE,
            channel="test",
        )
    )


def say(stack: Stack, run: Run, text: str) -> TurnResult:
    event = InboundEvent(
        channel="test", tenant=TENANT, user_id=USER, session_id=run.session_id, text=text
    )
    return handle(stack, event, scopes=READ_SCOPES, run=run)


def approve(stack: Stack, run: Run, parked: TurnResult) -> None:
    """The Slack path: the coordinator binds what the wait recorded."""
    assert parked.status == "awaiting_approval", parked.text
    assert parked.pending_wait is not None
    stack.coordinator.apply(reply(run, parked.pending_wait))


def times_asked(stack: Stack, run: Run) -> int:
    waits = (*stack.waits.pending_for(run.run_id), *stack.waits.satisfied_for(run.run_id))
    return sum(1 for w in waits if w.kind == "human_approval")


def test_a_read_between_approval_and_resume_does_not_ask_again(
    stack: Stack, rollout_run: Run
) -> None:
    """The auditor's reproduction: park, approve, read the history, resume."""
    run = rollout_run
    approve(stack, run, say(stack, run, ROLL_OUT))

    looked = say(stack, run, LOOK_AT_HISTORY)
    assert looked.observations, "the read has to happen for this to test anything"

    resumed = say(stack, run, ROLL_OUT)

    assert [i.kind for i in resumed.bundle.items].count("observation") == 1, (
        "the read is in the resume turn's context - which is what used to make it stale"
    )
    assert resumed.status == "complete", resumed.text
    assert len(stack.registry_client.rollouts) == 1
    assert times_asked(stack, run) == 1, "a human is asked once"


def test_a_turn_that_reads_before_it_proposes_commits_on_the_first_resume(
    stack: Stack, rollout_run: Run
) -> None:
    """The common case with a real model. The parking turn's own read is appended after
    it parks, so the first resume always saw one more observation than the ask did."""
    stack.deps.engine = ReadThenRollOutEngine()
    run = rollout_run

    parked = say(stack, run, ROLL_OUT)
    assert parked.observations, "the parking turn read before it proposed"
    approve(stack, run, parked)

    resumed = say(stack, run, ROLL_OUT)

    assert resumed.status == "complete", resumed.text
    assert len(stack.registry_client.rollouts) == 1
    assert times_asked(stack, run) == 1, "a human is asked once"


def test_the_request_fingerprint_leaves_out_what_the_session_read(
    stack: Stack, rollout_run: Run
) -> None:
    """The trace keeps the whole view; the approval binds the request. With nothing read
    they are one value, which is what keeps a wait parked before this change valid."""
    run = rollout_run

    def fingerprints(result: TurnResult) -> tuple[str, str]:
        span = next(s for s in result.tracer.spans if s.name == "context.assemble")
        return span.attributes["fingerprint"], span.attributes["request_fingerprint"]

    whole_1, request_1 = fingerprints(say(stack, run, LOOK_AT_HISTORY))
    whole_2, request_2 = fingerprints(say(stack, run, LOOK_AT_HISTORY))

    assert whole_1 == request_1, "nothing read yet, so nothing to leave out"
    assert whole_2 != whole_1, "the second turn was shown the first one's read"
    assert request_2 == request_1, "and was asked the same thing"


def test_a_different_payload_is_not_what_was_approved(stack: Stack, rollout_run: Run) -> None:
    """Leaving reads out loosens nothing else. Approving 10% does not roll out 25%."""
    run = rollout_run
    approve(stack, run, say(stack, run, ROLL_OUT))
    say(stack, run, LOOK_AT_HISTORY)

    wider = say(stack, run, ROLL_OUT.replace("percentage=10", "percentage=25"))

    assert wider.status == "awaiting_approval"
    assert wider.pending_request is not None
    assert wider.pending_request.payload["percentage"] == 25
    assert stack.registry_client.rollouts == []
    assert times_asked(stack, run) == 2, "a different act is a different question"


def test_an_approval_of_the_same_act_survives_a_differently_worded_request(
    stack: Stack, rollout_run: Run
) -> None:
    """Same act, different request: what the approval binds to is the world the act
    would change, not the message that produced the proposal (T43).

    An `ALWAYS`-tier commit runs as a Temporal activity handed ids alone (ADR-0008
    rule 3: no prompt in workflow history), so it has no request fingerprint left to
    compare against at the act - only the world, observed fresh through the gateway.
    Wording the same instruction differently is exactly the case an at-least-once
    retry produces, and it must not ask a person again for an act they already
    approved: the tool call `roll_out_variant_to_percentage` names is identical, so
    the approval, bound to that act and the world it was granted against, still
    applies. `test_a_different_payload_is_not_what_was_approved` is the case that
    still asks again: an actually different act.
    """
    run = rollout_run
    approve(stack, run, say(stack, run, ROLL_OUT))

    replaced = say(stack, run, f"{ROLL_OUT} and tell finance")

    assert replaced.status == "complete"
    assert len(stack.registry_client.rollouts) == 1
