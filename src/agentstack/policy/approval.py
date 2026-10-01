"""Approval, bound tightly enough that it means something (Identity, trust, policy, approvals).

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
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from agentstack.policy.envelope import IdentityEnvelope
from agentstack.policy.precommit import PreCommitPolicy
from agentstack.storage.database import Database
from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import Approval, ToolSpec


class GrantedBy(StrEnum):
    """Who said yes. Not a detail - it is the difference between an accountability
    record and a rule firing."""

    HUMAN = "human"
    POLICY = "policy"


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
    granted_by: GrantedBy = GrantedBy.HUMAN
    # Named when a policy granted it, absent when a person did.
    rule: str | None = None

    @property
    def by_a_human(self) -> bool:
        return self.granted_by is GrantedBy.HUMAN

    @staticmethod
    def of(row: tuple[Any, ...]) -> ApprovalRecord:
        """Build from a database row, coercing the grant kind.

        Postgres hands back a plain string, and `granted_by is GrantedBy.HUMAN` is
        False for one however equal it compares. An identity check that silently means
        "policy" for every stored human approval is the kind of bug that makes the
        strongest tier the easiest to satisfy.
        """
        return ApprovalRecord(
            id=str(row[0]),
            run_id=str(row[1]),
            action_fingerprint=str(row[2]),
            state_snapshot=str(row[3]),
            approver=str(row[4]),
            granted_at=row[5],
            summary=str(row[6]),
            granted_by=GrantedBy(row[7]),
            rule=None if row[8] is None else str(row[8]),
        )


_COLUMNS = (
    "id, run_id, action_fingerprint, state_snapshot, approver, granted_at, summary, "
    "granted_by, rule"
)


@dataclass(frozen=True, slots=True)
class ApprovalStore:
    db: Database

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
        trail that cannot answer who decided. The database restates both as CHECK
        constraints, because these two are worth holding twice.
        """
        if not summary.strip():
            raise ValueError(
                "an approval needs the summary the approver was actually shown; "
                "an empty one records agreement to nothing"
            )
        if not approver.strip():
            raise ValueError("an approval needs a named approver")
        return self.grant_for_fingerprint(
            run_id=run_id,
            action_fingerprint=request.fingerprint(),
            state_snapshot=state_snapshot,
            approver=approver,
            summary=summary,
        )

    def grant_for_fingerprint(
        self,
        *,
        run_id: str,
        action_fingerprint: str,
        state_snapshot: str,
        approver: str,
        summary: str,
    ) -> ApprovalRecord:
        """Grant against a fingerprint computed elsewhere.

        The Slack path needs this. The process that prepared the action recorded its
        fingerprint on the wait; the process handling the click an hour later has no
        `ActionRequest` to ask, and re-deriving one would mean approving whatever *this*
        process computes rather than what the approver was actually shown.
        """
        row = self.db.fetch_one(
            "INSERT INTO approvals"
            " (id, run_id, action_fingerprint, state_snapshot, approver, granted_at, summary)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (
                str(uuid.uuid4()),
                run_id,
                action_fingerprint,
                state_snapshot,
                approver,
                datetime.now(UTC),
                summary,
            ),
        )
        assert row is not None  # RETURNING on a successful insert always yields a row
        return ApprovalRecord.of(row)

    def grant_by_policy(
        self,
        *,
        run_id: str,
        request: ActionRequest,
        state_snapshot: str,
        rule: str,
        summary: str,
    ) -> ApprovalRecord:
        """A permission minted by a rule, recorded as one.

        The approver is `policy:<rule>` and never a person's name. An audit trail that
        cannot distinguish "a rule permitted this" from "someone decided this" cannot
        answer the only question it exists for.
        """
        row = self.db.fetch_one(
            "INSERT INTO approvals"
            " (id, run_id, action_fingerprint, state_snapshot, approver, granted_at,"
            "  summary, granted_by, rule)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, 'policy', %s) RETURNING {_COLUMNS}",
            (
                str(uuid.uuid4()),
                run_id,
                request.fingerprint(),
                state_snapshot,
                f"policy:{rule}",
                datetime.now(UTC),
                summary,
                rule,
            ),
        )
        assert row is not None  # RETURNING on a successful insert always yields a row
        return ApprovalRecord.of(row)

    def find(
        self,
        *,
        run_id: str,
        action_fingerprint: str,
        state_snapshot: str | None = None,
    ) -> ApprovalRecord | None:
        """The approval that authorizes this action, most recent first.

        When a state snapshot is given, an approval granted against *that* state wins.
        Without this, a run that was approved twice for the same action would read as
        stale on the approval that actually matches - reporting staleness that is not
        there, which is the fastest way to teach people to ignore it.

        Ordered by `seq`, not by `granted_at`: two approvals in the same microsecond
        are possible, and "most recent" has to have one answer.
        """
        row = self.db.fetch_one(
            f"SELECT {_COLUMNS} FROM approvals"
            " WHERE run_id = %s AND action_fingerprint = %s"
            " ORDER BY (state_snapshot = %s) DESC NULLS LAST, seq DESC"
            " LIMIT 1",
            (run_id, action_fingerprint, state_snapshot),
        )
        return None if row is None else ApprovalRecord.of(row)


def require_approval(
    *,
    store: ApprovalStore,
    spec: ToolSpec,
    request: ActionRequest,
    run_id: str,
    state_snapshot: str,
    envelope: IdentityEnvelope | None = None,
    policy: PreCommitPolicy | None = None,
) -> ApprovalRecord | None:
    """Return the approval that authorizes this action, or refuse.

    Three tiers, three behaviours:

    * `NONE` - nothing to check.
    * `PRE_COMMIT` - a rule may permit it and nobody is woken. The grant is recorded
      with the rule that made it, so the action is as auditable as a human one.
    * `ALWAYS` - a person, every time. **A policy grant never satisfies this tier.**
      Without that, a rule could authorise an irreversible act by minting the record
      the tier demands, and the strongest tier would be the easiest to satisfy.
    """
    if spec.approval is Approval.NONE:
        return None

    record = store.find(
        run_id=run_id,
        action_fingerprint=request.fingerprint(),
        state_snapshot=state_snapshot,
    )
    if record is not None and record.state_snapshot != state_snapshot:
        raise ApprovalStale(
            f"approval {record.id} was granted against a different state; re-ask before resuming"
        )

    if spec.approval is Approval.ALWAYS:
        if record is None:
            raise ApprovalRequired(
                f"{spec.name} on {request.resource} needs approval bound to this exact action"
            )
        if not record.by_a_human:
            raise ApprovalRequired(
                f"{spec.name} is {Approval.ALWAYS.value} and approval {record.id} was granted "
                f"by {record.rule!r}, not by a person. A rule does not get to authorise an "
                "irreversible act by minting the record the tier demands."
            )
        return record

    # PRE_COMMIT: an existing grant of either kind stands; otherwise ask the rule.
    if record is not None:
        return record
    if envelope is None or policy is None:
        raise ApprovalRequired(
            f"{spec.name} is {Approval.PRE_COMMIT.value} and no policy was supplied to "
            "evaluate it; a tier with nothing to consult cannot grant anything"
        )
    grant = policy.permit(spec=spec, request=request, envelope=envelope)
    if grant is None:
        reasons = "; ".join(policy.refusals(spec=spec, request=request, envelope=envelope))
        raise ApprovalRequired(
            f"{spec.name} on {request.resource} was not permitted by {policy.name}: {reasons}"
        )
    return store.grant_by_policy(
        run_id=run_id,
        request=request,
        state_snapshot=state_snapshot,
        rule=grant.rule,
        summary=grant.reason,
    )
