"""Part 6: every capability declares who acts, where, how reversibly, and at what price."""

from __future__ import annotations

import dataclasses

import pytest

from agentstack.tools.catalog import build_registry
from agentstack.tools.spec import (
    REQUIRED_TOOL_FIELDS,
    ActsAs,
    Approval,
    Idempotency,
    Surface,
    ToolSpec,
)

REGISTRY = build_registry()


def test_every_registered_tool_declares_complete_metadata() -> None:
    declared = {f.name for f in dataclasses.fields(ToolSpec)}
    assert declared >= REQUIRED_TOOL_FIELDS
    for spec in REGISTRY.specs():
        for field_name in REQUIRED_TOOL_FIELDS:
            assert getattr(spec, field_name) not in (None, "", frozenset()), (
                f"{spec.name}: {field_name} is part of the capability surface"
            )


def test_irreversible_tools_are_always_gated() -> None:
    for spec in REGISTRY.specs():
        if spec.side_effecting and not spec.reversible:
            assert spec.approval is Approval.ALWAYS, f"{spec.name} is irreversible and ungated"


def test_side_effecting_tools_declare_an_idempotency_policy() -> None:
    for spec in REGISTRY.specs():
        if spec.side_effecting:
            assert spec.idempotency is not Idempotency.NONE, f"{spec.name} cannot be retried safely"


def test_a_tool_without_an_approval_tier_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="needs an approval tier"):
        ToolSpec(
            name="send_email",
            description="send anything to anyone",
            input_schema={"type": "object"},
            acts_as=ActsAs.SERVICE,
            scope="mail:send",
            surface=Surface.MAIL,
            side_effecting=True,
            reversible=False,
            approval=Approval.NONE,
            idempotency=Idempotency.KEY,
        )


def test_an_irreversible_tool_cannot_settle_for_pre_commit_approval() -> None:
    with pytest.raises(ValueError, match="require approval=ALWAYS"):
        ToolSpec(
            name="drop_table",
            description="irreversible",
            input_schema={"type": "object"},
            acts_as=ActsAs.SERVICE,
            scope="db:admin",
            surface=Surface.DATABASE,
            side_effecting=True,
            reversible=False,
            approval=Approval.PRE_COMMIT,
            idempotency=Idempotency.KEY,
        )


def test_exposure_is_filtered_per_run() -> None:
    triage = {s.name for s in REGISTRY.expose_for(tenant="acme", stage="triage")}
    default = {s.name for s in REGISTRY.expose_for(tenant="acme", stage="default")}
    assert "issue_refund" not in triage, "blast radius: the refund tool is not a triage tool"
    assert "issue_refund" in default
    assert triage < default


def test_a_tool_that_was_not_exposed_cannot_be_prepared() -> None:
    from agentstack.tools.registry import ToolNotExposed

    exposed = REGISTRY.expose_for(tenant="acme", stage="triage")
    with pytest.raises(ToolNotExposed):
        REGISTRY.prepare(
            "issue_refund",
            {"tenant": "acme", "customer_id": "c", "charge_id": "x", "amount_cents": 1},
            exposed=exposed,
        )
