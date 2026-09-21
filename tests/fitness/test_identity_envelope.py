"""Part 7: every side effect carries a complete, narrow, expiring identity envelope."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, handle
from agentstack.policy.decisions import PolicyDenied, decide
from agentstack.policy.envelope import REQUIRED_ENVELOPE_FIELDS, IdentityEnvelope
from agentstack.runtime.run import Run
from agentstack.tools.catalog import REFUND, _refund
from agentstack.tools.spec import ActsAs

from .conftest import SCOPES, approve_and_resume

ARGS = {"tenant": "acme", "customer_id": "c-1", "charge_id": "ch-1", "amount_cents": 100}


def _envelope(
    *,
    principal: str = "u-1",
    acts_as: ActsAs = ActsAs.DELEGATED,
    tenant: str = "acme",
    delegation_scopes: frozenset[str] = frozenset({"billing:refund"}),
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
        spec=REFUND,
        request=_refund(ARGS),
    )
    assert not decision.allowed and decision.rule == "envelope.expired"


def test_policy_refuses_a_missing_scope() -> None:
    decision = decide(
        envelope=_envelope(delegation_scopes=frozenset({"billing:read"})),
        spec=REFUND,
        request=_refund(ARGS),
    )
    assert not decision.allowed and decision.rule == "scope.missing"


def test_policy_refuses_across_the_tenant_boundary() -> None:
    decision = decide(
        envelope=_envelope(tenant="globex"),
        spec=REFUND,
        request=_refund(ARGS),
    )
    assert not decision.allowed and decision.rule == "tenant.boundary"


def test_policy_refuses_the_wrong_acting_identity() -> None:
    decision = decide(
        envelope=_envelope(acts_as=ActsAs.SERVICE), spec=REFUND, request=_refund(ARGS)
    )
    assert not decision.allowed and decision.rule == "identity.mismatch"


def test_a_run_without_the_scope_is_refused_at_the_gateway(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    with pytest.raises(PolicyDenied, match="billing:refund"):
        handle(stack, event, scopes=frozenset({"billing:read"}), run=run)
