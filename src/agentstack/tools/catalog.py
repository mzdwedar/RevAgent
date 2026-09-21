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

from agentstack.tools.action import ActionRequest
from agentstack.tools.registry import Registry
from agentstack.tools.spec import ActsAs, Approval, Idempotency, Surface, ToolSpec

LOOKUP = ToolSpec(
    name="lookup_subscription",
    description="Read one customer's current subscription state.",
    input_schema={
        "type": "object",
        "properties": {"tenant": {"type": "string"}, "customer_id": {"type": "string"}},
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
            "tenant": {"type": "string"},
            "customer_id": {"type": "string"},
            "charge_id": {"type": "string"},
            "amount_cents": {"type": "integer"},
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
    stages=frozenset({"default"}),
)


def _lookup(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = str(arguments["tenant"])
    customer = str(arguments["customer_id"])
    return ActionRequest(
        tool=LOOKUP.name,
        surface=LOOKUP.surface,
        resource=f"{tenant}/customers/{customer}/subscription",
        payload={},
        idempotency_key=f"lookup:{tenant}:{customer}",
    )


def _refund(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = str(arguments["tenant"])
    customer = str(arguments["customer_id"])
    charge = str(arguments["charge_id"])
    amount = int(arguments["amount_cents"])
    return ActionRequest(
        tool=REFUND.name,
        surface=REFUND.surface,
        resource=f"{tenant}/customers/{customer}/charges/{charge}",
        payload={"amount_cents": amount},
        # The key is the business identity of the effect, not a random uuid:
        # a retry has to produce the same key or it is not a retry.
        idempotency_key=f"refund:{tenant}:{charge}:{amount}",
    )


def build_registry() -> Registry:
    registry = Registry()
    registry.register(LOOKUP, _lookup)
    registry.register(REFUND, _refund)
    return registry
