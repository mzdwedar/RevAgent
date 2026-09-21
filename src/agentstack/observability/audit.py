"""Accountability records, kept apart from debug traces (Part 8).

A trace shows that a tool executed. An audit record shows which identity authorized
it, under what policy, with what scope, and what changed. Burying the second inside
the first loses the access controls and retention the second needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class AuditRecord:
    run_id: str
    principal: str
    tenant: str
    action_fingerprint: str
    surface: str
    resource: str
    policy_decision: str
    approval_id: str | None
    outcome: str
    at: datetime


@dataclass(slots=True)
class AuditSink:
    """A separate sink with its own retention and access rules."""

    records: list[AuditRecord] = field(default_factory=list)

    def write(
        self,
        *,
        run_id: str,
        principal: str,
        tenant: str,
        action_fingerprint: str,
        surface: str,
        resource: str,
        policy_decision: str,
        approval_id: str | None,
        outcome: str,
    ) -> AuditRecord:
        record = AuditRecord(
            run_id=run_id,
            principal=principal,
            tenant=tenant,
            action_fingerprint=action_fingerprint,
            surface=surface,
            resource=resource,
            policy_decision=policy_decision,
            approval_id=approval_id,
            outcome=outcome,
            at=datetime.now(UTC),
        )
        self.records.append(record)
        return record

    def for_run(self, run_id: str) -> list[AuditRecord]:
        return [r for r in self.records if r.run_id == run_id]
