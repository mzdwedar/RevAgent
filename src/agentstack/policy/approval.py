"""Approval, bound tightly enough that it means something (Part 7).

An approval records four things, and all four are load-bearing:

* the **run** it belongs to, so it cannot be replayed into a different run
* the **action fingerprint**, so approving a draft does not approve a different send
* the **state snapshot** it was shown against, so a stale approval cannot resume into
  a changed world
* the **approver**, so the audit trail names a person

A modal that asks "proceed with the task?" at the start of a run satisfies none of
these. Approval sits immediately before the irreversible act.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import Approval, ToolSpec


class ApprovalRequired(PermissionError):
    """No approval exists for this exact action in this run."""


class ApprovalStale(PermissionError):
    """An approval exists, but the world it was granted against has changed."""


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: str
    run_id: str
    action_fingerprint: str
    state_snapshot: str
    approver: str
    granted_at: datetime
    summary: str


@dataclass(slots=True)
class ApprovalStore:
    _records: list[ApprovalRecord] = field(default_factory=list)

    def grant(
        self,
        *,
        run_id: str,
        request: ActionRequest,
        state_snapshot: str,
        approver: str,
        summary: str,
    ) -> ApprovalRecord:
        """What a human said yes to. `summary` is what they were shown.

        Both fields are required and both are load-bearing. An empty summary records
        that someone agreed to a blank screen; an unnamed approver leaves an audit
        trail that cannot answer who decided.
        """
        if not summary.strip():
            raise ValueError(
                "an approval needs the summary the approver was actually shown; "
                "an empty one records agreement to nothing"
            )
        if not approver.strip():
            raise ValueError("an approval needs a named approver")
        record = ApprovalRecord(
            id=str(uuid.uuid4()),
            run_id=run_id,
            action_fingerprint=request.fingerprint(),
            state_snapshot=state_snapshot,
            approver=approver,
            granted_at=datetime.now(UTC),
            summary=summary,
        )
        self._records.append(record)
        return record

    def find(self, *, run_id: str, action_fingerprint: str) -> ApprovalRecord | None:
        for record in self._records:
            if record.run_id == run_id and record.action_fingerprint == action_fingerprint:
                return record
        return None


def require_approval(
    *,
    store: ApprovalStore,
    spec: ToolSpec,
    request: ActionRequest,
    run_id: str,
    state_snapshot: str,
) -> ApprovalRecord | None:
    """Return the approval that authorizes this action, or refuse.

    Returns None only when the tool genuinely needs no approval.
    """
    if spec.approval is Approval.NONE:
        return None
    record = store.find(run_id=run_id, action_fingerprint=request.fingerprint())
    if record is None:
        raise ApprovalRequired(
            f"{spec.name} on {request.resource} needs approval bound to this exact action"
        )
    if record.state_snapshot != state_snapshot:
        raise ApprovalStale(
            f"approval {record.id} was granted against a different state; re-ask before resuming"
        )
    return record
