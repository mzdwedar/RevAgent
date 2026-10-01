"""Accountability records, kept apart from debug traces (Observability, evaluation, feedback).

A trace shows that a tool executed. An audit record shows which identity authorized
it, under what policy, with what scope, and what changed. Burying the second inside
the first loses the access controls and retention the second needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from agentstack.storage.database import Database


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
    # Set when the record is about a wait: a person's answer to it, or a refusal at the
    # act that never reached the gateway. The gateway's own records are about an action.
    wait_id: str | None = None
    state_snapshot: str | None = None


_COLUMNS = (
    "run_id, principal, tenant, action_fingerprint, surface, resource, "
    "policy_decision, approval_id, outcome, at, wait_id, state_snapshot"
)


@dataclass(frozen=True, slots=True)
class AuditSink:
    """A separate sink with its own retention and access rules.

    Separate in the schema too: `audit.records`, not `public.audit_records`. And with
    no foreign key onto `runs`, unlike every other table - the operational record can
    be deleted, and the accountability record must not go with it.
    """

    db: Database

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
        wait_id: str | None = None,
        state_snapshot: str | None = None,
    ) -> AuditRecord:
        row = self.db.fetch_one(
            f"INSERT INTO audit.records ({_COLUMNS})"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (
                run_id,
                principal,
                tenant,
                action_fingerprint,
                surface,
                resource,
                policy_decision,
                approval_id,
                outcome,
                datetime.now(UTC),
                wait_id,
                state_snapshot,
            ),
        )
        assert row is not None  # RETURNING on a successful insert always yields a row
        return AuditRecord(*row)

    def for_run(self, run_id: str) -> list[AuditRecord]:
        rows = self.db.fetch_all(
            f"SELECT {_COLUMNS} FROM audit.records WHERE run_id = %s ORDER BY id", (run_id,)
        )
        return [AuditRecord(*row) for row in rows]
