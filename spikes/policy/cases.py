"""Every combination of the inputs the authority decision reads, and the Python verdict.

The reference is the real code: `decide` then `require_approval`, in the gateway's order,
with the approval store replaced by one that answers per case.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agentstack.policy.approval import (
    ApprovalRecord,
    ApprovalRequired,
    ApprovalStale,
    GrantedBy,
    require_approval,
)
from agentstack.policy.decisions import decide
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.policy.precommit import PreCommitPolicy
from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import ActsAs, Approval, Surface

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
SNAPSHOT = "S"
# The order the Python code reaches its refusals in. Cedar and Rego report every
# refusal that applies; the audit record's `policy_decision` names the first.
ORDER = (
    "envelope.expired",
    "identity.mismatch",
    "scope.missing",
    "tenant.boundary",
    "approval.stale",
    "approval.required",
)


@dataclass(frozen=True)
class Case:
    live: bool
    acts_match: bool
    scope_ok: bool
    in_tenant: bool
    tier: str  # none | pre_commit | always
    reversible: bool
    record: str  # none | human | policy
    snap_match: bool

    # -- the raw facts each engine receives -------------------------------------
    def facts(self) -> dict:
        return {
            "now": int(NOW.timestamp()),
            "state_snapshot": SNAPSHOT,
            "envelope": {
                "tenant": "acme",
                "acts_as": "delegated",
                "scopes": ["billing:refund"] if self.scope_ok else ["billing:read"],
                "expires_at": int((NOW + timedelta(hours=1 if self.live else -1)).timestamp()),
            },
            "tool": {
                "acts_as": "delegated" if self.acts_match else "service",
                "scope": "billing:refund",
                "tier": self.tier,
                "reversible": self.reversible,
            },
            "request": {
                "resource": ("acme" if self.in_tenant else "globex") + "/customers/c-42",
            },
            "approval": {
                "exists": self.record != "none",
                "granted_by": self.record,
                "snapshot": SNAPSHOT if self.snap_match else "OLD",
            },
        }


def all_cases() -> list[Case]:
    out = []
    for live, acts, scope, tenant, tier, rev, rec in itertools.product(
        (True, False),
        (True, False),
        (True, False),
        (True, False),
        ("none", "pre_commit", "always"),
        (True, False),
        ("none", "human", "policy"),
    ):
        for snap in (True,) if rec == "none" else (True, False):
            out.append(Case(live, acts, scope, tenant, tier, rev, rec, snap))
    return out


class _Store:
    def __init__(self, case: Case) -> None:
        self.case = case

    def find(self, *, run_id, action_fingerprint, state_snapshot=None):
        del state_snapshot  # the case decides whether the record matches
        if self.case.record == "none":
            return None
        return ApprovalRecord(
            id="a",
            run_id=run_id,
            action_fingerprint=action_fingerprint,
            state_snapshot=SNAPSHOT if self.case.snap_match else "OLD",
            approver="x",
            granted_at=NOW,
            summary="s",
            granted_by=GrantedBy(self.case.record),
            rule="r" if self.case.record == "policy" else None,
        )

    def grant_by_policy(self, *, run_id, request, state_snapshot, rule, summary):
        del request
        return ApprovalRecord(
            id="new",
            run_id=run_id,
            action_fingerprint="f",
            state_snapshot=state_snapshot,
            approver=f"policy:{rule}",
            granted_at=NOW,
            summary=summary,
            granted_by=GrantedBy.POLICY,
            rule=rule,
        )


def python_verdict(case: Case) -> tuple[bool, str]:
    """(allowed, primary refusal or "")."""
    f = case.facts()
    envelope = IdentityEnvelope(
        principal="agent-operator",
        acts_as=ActsAs.DELEGATED,
        tenant="acme",
        delegation_scopes=frozenset(f["envelope"]["scopes"]),
        credential_ref="vault://x",
        expires_at=datetime.fromtimestamp(f["envelope"]["expires_at"], UTC),
        revocable=True,
    )
    # Duck-typed so combinations ToolSpec itself refuses (irreversible below ALWAYS)
    # are still asked: "should be unreachable" is what the defence in depth is for.
    spec = SimpleNamespace(
        name="issue_refund",
        acts_as=ActsAs(f["tool"]["acts_as"]),
        scope=f["tool"]["scope"],
        approval=Approval(case.tier),
        reversible=case.reversible,
    )
    request = ActionRequest(
        tool="issue_refund",
        surface=Surface.API,
        resource=f["request"]["resource"],
        payload={},
        idempotency_key="k",
    )
    d = decide(envelope=envelope, spec=spec, request=request, now=NOW)
    if not d.allowed:
        return False, d.rule
    try:
        require_approval(
            store=_Store(case),
            spec=spec,
            request=request,
            run_id="run",
            state_snapshot=SNAPSHOT,
            envelope=envelope,
            policy=PreCommitPolicy(),
        )
    except ApprovalStale:
        return False, "approval.stale"
    except ApprovalRequired:
        return False, "approval.required"
    return True, ""


def primary(reasons) -> str:
    return next((r for r in ORDER if r in set(reasons)), "")
