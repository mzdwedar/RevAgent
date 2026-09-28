"""This project's capability catalog.

Two tools, deliberately shaped to show the difference the metadata makes:

* `lookup_subscription` reads. No side effect, no approval, still scoped and still
  bound to an identity.
* `issue_refund` moves money. Irreversible, so `Approval.ALWAYS`; retried by the
  runtime, so an idempotency key; and narrow - it refunds one subscription rather
  than exposing a generic billing API.

Neither function performs I/O. They prepare an `ActionRequest`; the gateway commits it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agentstack.tools.action import ActionRequest, idempotency_key
from agentstack.tools.experiments import (
    ABSTAIN,
    DISCARD,
    DRAFT,
    GET,
    HALT,
    HISTORY,
    LIST,
    REVISE,
    ROLLOUT,
    prepare_abstain,
    prepare_discard,
    prepare_draft,
    prepare_get,
    prepare_halt,
    prepare_history,
    prepare_list,
    prepare_revise,
    prepare_rollout,
)
from agentstack.tools.registry import Registry
from agentstack.tools.spec import ActsAs, Approval, Idempotency, Surface, ToolSpec

LOOKUP = ToolSpec(
    name="lookup_subscription",
    description="Read one customer's current subscription state.",
    input_schema={
        "type": "object",
        # Both become segments of the resource path, so both are single-segment ids.
        "properties": {
            "tenant": {"type": "string", "format": "id"},
            "customer_id": {"type": "string", "format": "id"},
        },
        "required": ["tenant", "customer_id"],
    },
    acts_as=ActsAs.DELEGATED,
    scope="billing:read",
    surface=Surface.API,
    side_effecting=False,
    reversible=True,
    approval=Approval.NONE,
    idempotency=Idempotency.NATURAL,
    stages=frozenset({"default", "triage"}),
)

REFUND = ToolSpec(
    name="issue_refund",
    description="Refund one charge on one subscription. Money leaves the account.",
    input_schema={
        "type": "object",
        "properties": {
            "tenant": {"type": "string", "format": "id"},
            "customer_id": {"type": "string", "format": "id"},
            "charge_id": {"type": "string", "format": "id"},
            "amount_cents": {"type": "integer", "format": "cents", "currency": "USD"},
        },
        "required": ["tenant", "customer_id", "charge_id", "amount_cents"],
    },
    acts_as=ActsAs.DELEGATED,
    scope="billing:refund",
    surface=Surface.API,
    side_effecting=True,
    reversible=False,
    approval=Approval.ALWAYS,
    idempotency=Idempotency.KEY,
    reversal_note="cannot be undone; reversing requires raising a new charge",
    stages=frozenset({"default"}),
)


def _lookup(arguments: Mapping[str, Any]) -> ActionRequest:
    # Arguments arrive validated against input_schema (tools/validation.py), so these
    # are the declared types, not hopeful coercions of model output.
    tenant = arguments["tenant"]
    customer = arguments["customer_id"]
    return ActionRequest(
        tool=LOOKUP.name,
        surface=LOOKUP.surface,
        resource=f"{tenant}/customers/{customer}/subscription",
        payload={},
        idempotency_key=idempotency_key("lookup", tenant, customer),
    )


def _refund(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    customer = arguments["customer_id"]
    charge = arguments["charge_id"]
    amount = arguments["amount_cents"]
    return ActionRequest(
        tool=REFUND.name,
        surface=REFUND.surface,
        resource=f"{tenant}/customers/{customer}/charges/{charge}",
        payload={"amount_cents": amount},
        # The key is the business identity of the effect, not a random uuid:
        # a retry has to produce the same key or it is not a retry.
        idempotency_key=idempotency_key("refund", tenant, charge, amount),
    )


def build_registry() -> Registry:
    """Every tool this system has, on the stages they belong to.

    The refund pair is the walkthrough: it exists so the stack is runnable end to end
    and so the layer invariants have something concrete to be asserted against. The
    experiment pair is what the operator actually does. They are on different stages,
    so no run is ever shown both - a drafting turn cannot see the rollout tool, and a
    refund turn cannot see either.
    """
    registry = Registry()
    registry.register(LOOKUP, _lookup)
    registry.register(REFUND, _refund)
    registry.register(DRAFT, prepare_draft)
    registry.register(ROLLOUT, prepare_rollout)
    registry.register(GET, prepare_get)
    registry.register(LIST, prepare_list)
    registry.register(HISTORY, prepare_history)
    registry.register(REVISE, prepare_revise)
    registry.register(DISCARD, prepare_discard)
    registry.register(ABSTAIN, prepare_abstain)
    registry.register(HALT, prepare_halt)
    return registry
