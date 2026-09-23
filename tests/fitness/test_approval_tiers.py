"""Criterion 20: PRE_COMMIT is not ALWAYS. Three tiers, three behaviours.

The defect this closes: `PRE_COMMIT` and `ALWAYS` were declared as different tiers and
implemented identically - both demanded a human-granted record. A tier nothing enforces
is a comment, and this one read as a promise that some actions proceed on policy alone.

The invariant that matters most here is the one in the other direction: **a policy
grant must never satisfy `ALWAYS`**. Without it a rule could authorise an irreversible
act by minting exactly the record the tier demands, and the strongest tier would become
the easiest to satisfy.
"""

from __future__ import annotations

import pytest

from agentstack.interfaces.wiring import Stack, envelope_for
from agentstack.observability.spans import Tracer
from agentstack.policy.approval import (
    ApprovalRequired,
    ApprovalStale,
    GrantedBy,
    require_approval,
)
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.policy.precommit import PreCommitPolicy
from agentstack.runtime.run import Run
from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.action import ActionRequest
from agentstack.tools.catalog import LOOKUP, REFUND, _refund
from agentstack.tools.spec import ActsAs, Approval, Idempotency, Surface, ToolSpec

from .conftest import SCOPES

ARGS = {"tenant": "acme", "customer_id": "c-42", "charge_id": "ch-7", "amount_cents": 1999}

DRAFT = ToolSpec(
    name="create_experiment_draft",
    description="Write a candidate experiment into the registry. Reversible: drafts are deleted.",
    input_schema={
        "type": "object",
        "properties": {"tenant": {"type": "string"}, "experiment_id": {"type": "string"}},
        "required": ["tenant", "experiment_id"],
    },
    acts_as=ActsAs.DELEGATED,
    scope="billing:read",
    surface=Surface.API,
    side_effecting=True,
    reversible=True,
    approval=Approval.PRE_COMMIT,
    idempotency=Idempotency.KEY,
    stages=frozenset({"default"}),
)


def draft_request(tenant: str = "acme") -> ActionRequest:
    return ActionRequest(
        tool=DRAFT.name,
        surface=Surface.API,
        # Inside the run's allowed prefixes: this exercises the approval tier, and a
        # containment refusal would stop it before the tier was reached.
        resource=f"{tenant}/customers/c-42/experiments/exp-7",
        payload={"experiment_id": "exp-7"},
        idempotency_key=f"draft:{tenant}:exp-7",
    )


def envelope(stack: Stack, run: Run) -> IdentityEnvelope:
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    return envelope_for(view, scopes=SCOPES)


# --- PRE_COMMIT proceeds on policy, with the rule on the record ---


def test_a_pre_commit_action_proceeds_without_a_human(stack: Stack, run: Run) -> None:
    record = require_approval(
        store=stack.approvals,
        spec=DRAFT,
        request=draft_request(),
        run_id=run.run_id,
        state_snapshot="s",
        envelope=envelope(stack, run),
        policy=PreCommitPolicy(),
    )

    assert record is not None
    assert record.granted_by is GrantedBy.POLICY
    assert record.by_a_human is False


def test_the_grant_names_the_rule_that_made_it(stack: Stack, run: Run) -> None:
    """An approval that cannot say which rule permitted it is unauditable."""
    record = require_approval(
        store=stack.approvals,
        spec=DRAFT,
        request=draft_request(),
        run_id=run.run_id,
        state_snapshot="s",
        envelope=envelope(stack, run),
        policy=PreCommitPolicy(),
    )

    assert record is not None
    assert record.rule == "pre_commit.reversible_within_tenant"
    assert record.approver == "policy:pre_commit.reversible_within_tenant"


def test_a_policy_grant_is_never_mistakable_for_a_person(stack: Stack, run: Run) -> None:
    """ "Who approved this" must have one answer, and it must not be a rule wearing a
    person's name."""
    policy_record = require_approval(
        store=stack.approvals,
        spec=DRAFT,
        request=draft_request(),
        run_id=run.run_id,
        state_snapshot="s",
        envelope=envelope(stack, run),
        policy=PreCommitPolicy(),
    )
    human_record = stack.approvals.grant(
        run_id=run.run_id,
        request=_refund(ARGS),
        state_snapshot="s",
        approver="finance-oncall",
        summary="refund 19.99",
    )

    assert policy_record is not None
    assert policy_record.approver.startswith("policy:")
    assert human_record.by_a_human is True
    assert human_record.rule is None


def test_the_database_refuses_a_policy_grant_with_no_rule(run: Run, app_database: Database) -> None:
    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(
            "INSERT INTO approvals (id, run_id, action_fingerprint, state_snapshot,"
            " approver, granted_at, summary, granted_by, rule)"
            " VALUES ('a-1', %s, 'fp', 's', 'policy:x', now(), 'x', 'policy', NULL)",
            (run.run_id,),
        )

    assert caught.value.constraint == "grants_name_their_source"


def test_the_database_refuses_a_human_grant_carrying_a_rule(
    run: Run, app_database: Database
) -> None:
    """A human approval with a rule name is a policy grant wearing a person's name."""
    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(
            "INSERT INTO approvals (id, run_id, action_fingerprint, state_snapshot,"
            " approver, granted_at, summary, granted_by, rule)"
            " VALUES ('a-2', %s, 'fp', 's', 'ana', now(), 'x', 'human', 'some.rule')",
            (run.run_id,),
        )

    assert caught.value.constraint == "grants_name_their_source"


# --- the tier that matters: ALWAYS is never satisfied by a rule ---


def test_a_policy_grant_does_not_satisfy_an_always_tool(stack: Stack, run: Run) -> None:
    """The invariant that keeps the strongest tier from becoming the easiest.

    The record exists, matches the run, the fingerprint and the state snapshot - every
    check the old implementation made. Only its source disqualifies it.
    """
    request = _refund(ARGS)
    stack.approvals.grant_by_policy(
        run_id=run.run_id,
        request=request,
        state_snapshot="s",
        rule="pre_commit.reversible_within_tenant",
        summary="a rule said so",
    )

    with pytest.raises(ApprovalRequired, match="not by a person"):
        require_approval(
            store=stack.approvals,
            spec=REFUND,
            request=request,
            run_id=run.run_id,
            state_snapshot="s",
            envelope=envelope(stack, run),
            policy=PreCommitPolicy(),
        )


def test_an_always_tool_still_accepts_a_human_approval(stack: Stack, run: Run) -> None:
    request = _refund(ARGS)
    granted = stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot="s",
        approver="finance-oncall",
        summary="refund 19.99",
    )

    found = require_approval(
        store=stack.approvals,
        spec=REFUND,
        request=request,
        run_id=run.run_id,
        state_snapshot="s",
        envelope=envelope(stack, run),
        policy=PreCommitPolicy(),
    )

    assert found == granted


def test_an_always_tool_with_no_approval_still_refuses(stack: Stack, run: Run) -> None:
    with pytest.raises(ApprovalRequired, match="bound to this exact action"):
        require_approval(
            store=stack.approvals,
            spec=REFUND,
            request=_refund(ARGS),
            run_id=run.run_id,
            state_snapshot="s",
            envelope=envelope(stack, run),
            policy=PreCommitPolicy(),
        )


def test_a_none_tier_needs_nothing(stack: Stack, run: Run) -> None:
    assert (
        require_approval(
            store=stack.approvals,
            spec=LOOKUP,
            request=draft_request(),
            run_id=run.run_id,
            state_snapshot="s",
        )
        is None
    )


# --- the policy can say no, which is what makes it a policy ---


def test_a_cross_tenant_action_is_refused_by_policy(stack: Stack, run: Run) -> None:
    """Containment would catch it later. Being refused *by policy* means the audit
    trail says the rule never permitted it, which is a different answer."""
    with pytest.raises(ApprovalRequired, match="not within tenant acme"):
        require_approval(
            store=stack.approvals,
            spec=DRAFT,
            request=draft_request(tenant="other"),
            run_id=run.run_id,
            state_snapshot="s",
            envelope=envelope(stack, run),
            policy=PreCommitPolicy(),
        )


def test_a_policy_with_no_checks_permits_nothing(stack: Stack, run: Run) -> None:
    """An empty rule set that permitted everything would be the same defect this task
    closes: something declared, nothing enforced."""
    with pytest.raises(ApprovalRequired, match="no checks"):
        require_approval(
            store=stack.approvals,
            spec=DRAFT,
            request=draft_request(),
            run_id=run.run_id,
            state_snapshot="s",
            envelope=envelope(stack, run),
            policy=PreCommitPolicy(checks=()),
        )


def test_a_pre_commit_tool_with_no_policy_is_refused(stack: Stack, run: Run) -> None:
    """A tier with nothing to consult cannot grant anything, and must not fall through
    to granting anyway."""
    with pytest.raises(ApprovalRequired, match="no policy was supplied"):
        require_approval(
            store=stack.approvals,
            spec=DRAFT,
            request=draft_request(),
            run_id=run.run_id,
            state_snapshot="s",
        )


def test_an_irreversible_tool_cannot_be_registered_as_pre_commit() -> None:
    """The first check in the policy is for a case `ToolSpec` already refuses. Both
    exist because "should be unreachable" is how the old tier was described too."""
    with pytest.raises(ValueError, match="(?i)approval"):
        ToolSpec(
            name="delete_everything",
            description="Irreversible and hopeful.",
            input_schema={"type": "object", "properties": {}, "required": []},
            acts_as=ActsAs.DELEGATED,
            scope="billing:refund",
            surface=Surface.API,
            side_effecting=True,
            reversible=False,
            approval=Approval.PRE_COMMIT,
            idempotency=Idempotency.KEY,
            stages=frozenset({"default"}),
        )


# --- staleness still applies to both tiers ---


def test_a_stale_policy_grant_is_refused_like_any_other(stack: Stack, run: Run) -> None:
    request = draft_request()
    stack.approvals.grant_by_policy(
        run_id=run.run_id,
        request=request,
        state_snapshot="as-shown",
        rule="pre_commit.reversible_within_tenant",
        summary="a rule said so",
    )

    with pytest.raises((ApprovalStale, ApprovalRequired)):
        require_approval(
            store=stack.approvals,
            spec=DRAFT,
            request=request,
            run_id=run.run_id,
            state_snapshot="the-world-moved",
            envelope=envelope(stack, run),
            policy=PreCommitPolicy(checks=()),
        )


def test_a_pre_commit_action_is_audited_through_the_gateway(stack: Stack, run: Run) -> None:
    """Criterion 20's other half: it proceeds *and* leaves a record naming the rule."""
    stack.deps.registry.register(DRAFT, lambda args: draft_request(str(args["tenant"])))
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)

    result = stack.deps.gateway.execute(
        request=draft_request(),
        spec=DRAFT,
        envelope=envelope(stack, run),
        run_id=run.run_id,
        state_snapshot="s",
        tracer=tracer,
    )

    audited = [r for r in stack.audit.for_run(run.run_id) if r.outcome == "committed"]
    assert result.receipt
    assert len(audited) == 1
    assert audited[0].policy_decision == "approval.policy:pre_commit.reversible_within_tenant"
    assert audited[0].approval_id
