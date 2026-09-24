"""Evaluate a case with Cedar."""

from __future__ import annotations

import json
from pathlib import Path

import cedarpy

HERE = Path(__file__).parent
_SRC = (HERE / "authority.cedar").read_text()
POLICIES = cedarpy.PolicySet.from_str(_SRC)
# cedarpy names policies policy0..N; the @id annotation is where the rule name lives.
RULE = {
    pid: p["annotations"]["id"]
    for pid, p in json.loads(cedarpy.policies_to_json_str(_SRC))["staticPolicies"].items()
}
SCHEMA = cedarpy.Schema.from_str((HERE / "authority.cedarschema").read_text())
ENTITIES = cedarpy.Entities.from_json_str("[]")


def context(facts: dict) -> dict:
    ctx = {k: v for k, v in facts.items() if k != "request"}
    # The one thing Cedar cannot do for itself: split the tenant off the resource.
    ctx["request"] = {"resource_tenant": facts["request"]["resource"].split("/", 1)[0]}
    return ctx


def evaluate(facts: dict, *, schema=SCHEMA, ctx: dict | None = None):
    return cedarpy.is_authorized(
        {
            "principal": 'Agent::"run"',
            "action": 'Action::"act"',
            "resource": 'Tool::"issue_refund"',
            "context": ctx if ctx is not None else context(facts),
        },
        POLICIES,
        ENTITIES,
        schema=schema,
    )


def verdict(facts: dict) -> tuple[bool, set[str], list]:
    r = evaluate(facts)
    rules = (RULE[p] for p in r.diagnostics.reasons)
    reasons = {x.split(".always")[0].split(".pre_commit")[0] for x in rules if x != "allow"}
    return r.allowed, reasons, list(r.diagnostics.errors)
