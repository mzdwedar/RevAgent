"""Criterion 19: a model-drafted candidate cannot bypass validation.

A proposal with a missing field, a wrong type, an extra argument, or a tool this run was
never shown is refused **before the registry is touched**, and the turn still answers.

Criterion 14 rides along: a rollout carries the cohort predicate as required arguments,
so it cannot be prepared without naming the population a human approved.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, envelope_for, handle
from agentstack.model.ollama_engine import OllamaEngine
from agentstack.observability.spans import Tracer
from agentstack.policy.approval import ApprovalRequired
from agentstack.runtime.run import Run, new_run
from agentstack.tools.experiments import (
    DRAFT,
    EXPERIMENT_STAGE,
    ROLLOUT,
    prepare_draft,
    prepare_rollout,
)
from agentstack.tools.registry import ToolNotExposed
from agentstack.tools.spec import Approval, Surface
from agentstack.tools.validation import InvalidToolArguments

from .conftest import TENANT, USER

SCOPES = frozenset({"experiments:write", "experiments:rollout"})

GOOD_DRAFT = {
    "tenant": TENANT,
    "experiment_id": "exp-7",
    "experiment_version": "exp:abc123",
    "hypothesis": "a discount retains at-risk customers",
    "variant": "20-percent-off",
}
GOOD_ROLLOUT = {
    "tenant": TENANT,
    "experiment_id": "exp-7",
    "experiment_version": "exp:abc123",
    "percentage": 10,
    "targeting_model_version": "tabpfn-3.5",
    "risk_threshold": 0.61,
}


def exposed(stack: Stack) -> Any:
    return stack.deps.registry.expose_for(tenant=TENANT, stage=EXPERIMENT_STAGE)


# --- the tiers, declared ---


def test_a_draft_is_reversible_and_decided_by_policy() -> None:
    assert DRAFT.reversible is True
    assert DRAFT.approval is Approval.PRE_COMMIT


def test_a_rollout_is_irreversible_and_wakes_a_person() -> None:
    assert ROLLOUT.reversible is False
    assert ROLLOUT.approval is Approval.ALWAYS
    assert "cannot be undone" in (ROLLOUT.reversal_note or "")


def test_the_reversal_note_says_what_reversing_does_not_undo() -> None:
    """ "Roll it back" sounds like undoing. It stops future exposure and does not unsee
    the offer - an approver weighing an irreversible act needs the difference."""
    note = ROLLOUT.reversal_note or ""

    assert "does not unsee" in note or "not unsee" in note


# --- the exposure filter separates drafting from rolling out ---


def test_a_refund_run_is_shown_neither_experiment_tool(stack: Stack) -> None:
    names = {s.name for s in stack.deps.registry.expose_for(tenant=TENANT, stage="default")}

    assert "create_experiment_draft" not in names
    assert "roll_out_variant_to_percentage" not in names


def test_an_experiment_run_is_shown_neither_refund_tool(stack: Stack) -> None:
    """Exposing every tool on every run is the cheapest way to hand an injection a menu."""
    names = {s.name for s in exposed(stack)}

    assert names == {"create_experiment_draft", "roll_out_variant_to_percentage"}


# --- criterion 19: refused before the registry is touched ---


@pytest.mark.parametrize("missing", sorted(GOOD_DRAFT))
def test_a_draft_missing_any_required_field_is_refused(stack: Stack, missing: str) -> None:
    arguments = {k: v for k, v in GOOD_DRAFT.items() if k != missing}

    with pytest.raises(InvalidToolArguments):
        stack.deps.registry.prepare(DRAFT.name, arguments, exposed=exposed(stack))

    assert stack.registry_client.drafts == {}, "the registry was written to anyway"


def test_a_wrong_type_is_refused(stack: Stack) -> None:
    """The model wrote the percentage as prose. The schema says integer."""
    with pytest.raises(InvalidToolArguments):
        stack.deps.registry.prepare(
            ROLLOUT.name, {**GOOD_ROLLOUT, "percentage": "ten"}, exposed=exposed(stack)
        )

    assert stack.registry_client.rollouts == []


def test_an_extra_argument_is_refused(stack: Stack) -> None:
    """A field nobody declared is a field nobody validated."""
    with pytest.raises(InvalidToolArguments):
        stack.deps.registry.prepare(
            DRAFT.name, {**GOOD_DRAFT, "bypass_approval": True}, exposed=exposed(stack)
        )

    assert stack.registry_client.drafts == {}


def test_a_tool_this_run_was_not_shown_is_refused(stack: Stack) -> None:
    """A proposal is not an entitlement."""
    refund_stage = stack.deps.registry.expose_for(tenant=TENANT, stage="default")

    with pytest.raises(ToolNotExposed):
        stack.deps.registry.prepare(ROLLOUT.name, GOOD_ROLLOUT, exposed=refund_stage)

    assert stack.registry_client.rollouts == []


def test_a_well_formed_draft_prepares(stack: Stack) -> None:
    """The counterpart: the refusals above must not be passing because nothing works."""
    request = stack.deps.registry.prepare(DRAFT.name, GOOD_DRAFT, exposed=exposed(stack))

    assert request.surface is Surface.REGISTRY
    assert request.resource == f"{TENANT}/experiments/exp-7"


# --- criterion 14: the rollout carries the cohort predicate ---


@pytest.mark.parametrize("field", ["targeting_model_version", "risk_threshold"])
def test_a_rollout_without_the_cohort_predicate_cannot_be_prepared(
    stack: Stack, field: str
) -> None:
    """An approval is granted against a specific frozen cohort. A rollout that
    re-derived the cohort at execution time could reach a different population than the
    one a human saw."""
    arguments = {k: v for k, v in GOOD_ROLLOUT.items() if k != field}

    with pytest.raises(InvalidToolArguments):
        stack.deps.registry.prepare(ROLLOUT.name, arguments, exposed=exposed(stack))


def test_the_cohort_predicate_travels_in_the_payload() -> None:
    request = prepare_rollout(GOOD_ROLLOUT)

    assert request.payload["targeting_model_version"] == "tabpfn-3.5"
    assert request.payload["risk_threshold"] == 0.61
    assert request.payload["experiment_version"] == "exp:abc123"


# --- idempotency keys are the identity of the effect ---


def test_two_rollout_percentages_are_two_effects() -> None:
    """10% and 25% are different things to do. If the second deduplicated against the
    first, widening a rollout would silently do nothing."""
    ten = prepare_rollout(GOOD_ROLLOUT)
    twenty_five = prepare_rollout({**GOOD_ROLLOUT, "percentage": 25})

    assert ten.idempotency_key != twenty_five.idempotency_key


def test_the_same_draft_for_the_same_frozen_cohort_is_one_effect() -> None:
    assert prepare_draft(GOOD_DRAFT).idempotency_key == prepare_draft(GOOD_DRAFT).idempotency_key


def test_a_draft_against_a_different_cohort_is_a_different_effect() -> None:
    """Drafting the same hypothesis about a different population is a different
    candidate, and must not deduplicate into the first."""
    other = prepare_draft({**GOOD_DRAFT, "experiment_version": "exp:moved"})

    assert prepare_draft(GOOD_DRAFT).idempotency_key != other.idempotency_key


# --- through the whole stack ---


def experiment_run(stack: Stack) -> Run:
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    return stack.runs.ensure(
        new_run(
            session_id=session.session_id,
            tenant=TENANT,
            user=USER,
            stage=EXPERIMENT_STAGE,
            channel="test",
        )
    )


def test_a_draft_commits_on_a_policy_grant_with_no_human(stack: Stack) -> None:
    """PRE_COMMIT, end to end: the registry is written and nobody was woken."""
    run = experiment_run(stack)
    view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)

    result = stack.deps.gateway.execute(
        request=prepare_draft(GOOD_DRAFT),
        spec=DRAFT,
        envelope=envelope_for(view, scopes=SCOPES),
        run_id=run.run_id,
        state_snapshot="s",
        tracer=tracer,
    )

    assert result.receipt
    assert f"{TENANT}/experiments/exp-7" in stack.registry_client.drafts
    audited = [r for r in stack.audit.for_run(run.run_id) if r.outcome == "committed"]
    assert audited[0].policy_decision.startswith("approval.policy:")


def test_a_rollout_refuses_without_a_human(stack: Stack) -> None:
    """ALWAYS, end to end: no policy rule reaches this one."""
    run = experiment_run(stack)
    view = stack.resolver.resolve(session_id=run.session_id, user_id=USER, tenant=TENANT)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)

    with pytest.raises(ApprovalRequired):
        stack.deps.gateway.execute(
            request=prepare_rollout(GOOD_ROLLOUT),
            spec=ROLLOUT,
            envelope=envelope_for(view, scopes=SCOPES),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=tracer,
        )

    assert stack.registry_client.rollouts == [], "an unapproved rollout reached the surface"


def test_a_malformed_model_proposal_is_refused_and_the_turn_answers(stack: Stack) -> None:
    """Criterion 19 through a real adapter: the model drafted a candidate with the
    version missing, and the turn still produced an answer."""
    run = experiment_run(stack)
    stack.deps.engine = OllamaEngine(
        build_client=lambda host: _Canned(
            host,
            {
                "message": {
                    "content": "drafting the candidate",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "create_experiment_draft",
                                "arguments": {
                                    k: v for k, v in GOOD_DRAFT.items() if k != "experiment_version"
                                },
                            }
                        }
                    ],
                }
            },
        )
    )
    event = InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=run.session_id,
        text="draft a retention experiment",
    )

    result = handle(stack, event, scopes=SCOPES, run=run)

    assert result.status == "rejected"
    assert "tool.reject" in result.tracer.names()
    assert result.text
    assert stack.registry_client.drafts == {}


class _Canned:
    def __init__(self, host: str, answer: dict[str, Any]) -> None:
        self.host = host
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.answer
