"""Identity, trust, policy, approvals: every side effect carries a complete, narrow, expiring
identity envelope."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.policy.decisions import PolicyDenied, decide
from agentstack.policy.envelope import REQUIRED_ENVELOPE_FIELDS, IdentityEnvelope
from agentstack.runtime.run import Run
from agentstack.tools.experiments import (
    GET,
    HALT,
    LIST,
    ROLLOUT,
    prepare_get,
    prepare_halt,
    prepare_list,
    prepare_rollout,
)
from agentstack.tools.spec import ActsAs

from .conftest import ROLLOUT_ARGS, SCOPES, approve_and_resume

ARGS = ROLLOUT_ARGS


def _envelope(
    *,
    principal: str = "u-1",
    acts_as: ActsAs = ActsAs.DELEGATED,
    tenant: str = "acme",
    delegation_scopes: frozenset[str] = frozenset({"experiments:rollout"}),
    expires_at: datetime | None = None,
) -> IdentityEnvelope:
    return IdentityEnvelope(
        principal=principal,
        acts_as=acts_as,
        tenant=tenant,
        delegation_scopes=delegation_scopes,
        credential_ref="vault://x",
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
        revocable=True,
    )


def test_the_envelope_declares_every_required_dimension() -> None:
    declared = {f.name for f in dataclasses.fields(IdentityEnvelope)}
    assert declared >= REQUIRED_ENVELOPE_FIELDS


def test_an_envelope_with_no_scopes_is_refused() -> None:
    with pytest.raises(ValueError, match="god token"):
        _envelope(delegation_scopes=frozenset())


def test_policy_refuses_an_expired_credential() -> None:
    decision = decide(
        envelope=_envelope(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
        spec=ROLLOUT,
        request=prepare_rollout(ARGS),
    )
    assert not decision.allowed and decision.rule == "envelope.expired"


def test_policy_refuses_a_missing_scope() -> None:
    decision = decide(
        envelope=_envelope(delegation_scopes=frozenset({"experiments:read"})),
        spec=ROLLOUT,
        request=prepare_rollout(ARGS),
    )
    assert not decision.allowed and decision.rule == "scope.missing"


def test_policy_refuses_across_the_tenant_boundary() -> None:
    decision = decide(
        envelope=_envelope(tenant="globex"),
        spec=ROLLOUT,
        request=prepare_rollout(ARGS),
    )
    assert not decision.allowed and decision.rule == "tenant.boundary"


def test_policy_refuses_the_wrong_acting_identity() -> None:
    decision = decide(
        envelope=_envelope(acts_as=ActsAs.SERVICE), spec=ROLLOUT, request=prepare_rollout(ARGS)
    )
    assert not decision.allowed and decision.rule == "identity.mismatch"


HALT_ARGS = {
    "tenant": "acme",
    "experiment_version": "exp:abc123",
    "reason": "churn rose in the variant",
}
HALT_SCOPES = frozenset({"experiments:read", "experiments:halt"})


def test_a_subject_bound_envelope_refuses_another_experiments_halt() -> None:
    bound = _envelope(delegation_scopes=HALT_SCOPES).bound_to("acme/experiments/exp-7")

    other = decide(
        envelope=bound, spec=HALT, request=prepare_halt({**HALT_ARGS, "experiment_id": "exp-9"})
    )
    own = decide(
        envelope=bound, spec=HALT, request=prepare_halt({**HALT_ARGS, "experiment_id": "exp-7"})
    )

    assert not other.allowed and other.rule == "subject.boundary"
    assert own.allowed


def test_the_subject_is_a_path_segment_not_a_string_prefix() -> None:
    """`exp-7` must not cover `exp-70`."""
    bound = _envelope(delegation_scopes=HALT_SCOPES).bound_to("acme/experiments/exp-7")
    decision = decide(
        envelope=bound, spec=HALT, request=prepare_halt({**HALT_ARGS, "experiment_id": "exp-70"})
    )
    assert not decision.allowed and decision.rule == "subject.boundary"


def test_the_subject_bounds_writes_and_leaves_reads_alone() -> None:
    """Deliberate: a read changes nothing, and the list read's resource is the
    collection, which no one experiment contains (see `outside_subject`)."""
    bound = _envelope(delegation_scopes=HALT_SCOPES).bound_to("acme/experiments/exp-7")
    for spec, request in (
        (GET, prepare_get({"tenant": "acme", "experiment_id": "exp-9"})),
        (LIST, prepare_list({"tenant": "acme"})),
    ):
        assert decide(envelope=bound, spec=spec, request=request).allowed


def test_a_bound_envelope_can_only_narrow() -> None:
    bound = _envelope().bound_to("acme/experiments/exp-7")
    assert bound.bound_to("acme/experiments/exp-7") == bound
    with pytest.raises(ValueError, match="cannot be re-bound"):
        bound.bound_to("acme/experiments/exp-9")
    with pytest.raises(ValueError, match="outside tenant"):
        _envelope().bound_to("globex/experiments/exp-7")


def test_no_human_grant_carries_a_bound_run_past_its_subject(stack: Stack, run: Run) -> None:
    """In `decide`, not the `PRE_COMMIT` rules: a person approving exactly this
    request - another experiment's halt - changes nothing, and the refusal is audited."""
    request = prepare_halt({**HALT_ARGS, "experiment_id": "exp-9"})
    stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot="s",
        approver="a distracted human",
        summary="looked fine",
    )
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)

    with pytest.raises(PolicyDenied, match="bound to acme/experiments/exp-7"):
        stack.deps.gateway.execute(
            request=request,
            spec=HALT,
            envelope=envelope_for(view, scopes=HALT_SCOPES).bound_to("acme/experiments/exp-7"),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=Tracer(
                run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions
            ),
        )

    denied = [r for r in stack.audit.for_run(run.run_id) if r.outcome == "denied"]
    assert [r.policy_decision for r in denied] == ["subject.boundary"]


def test_a_run_without_the_scope_is_refused_at_the_gateway(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    with pytest.raises(PolicyDenied, match="experiments:rollout"):
        handle(stack, event, scopes=frozenset({"experiments:read"}), run=run)
