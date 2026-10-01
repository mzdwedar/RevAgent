"""Who may approve, and for whom (Identity, trust, policy, approvals, criterion 25).

The Slack adapter reads a user id off an interaction and passes it on. It has no
opinion about whether that person may approve anything, and it should not: authorising
at the transport boundary puts the decision in the layer least able to audit it, and
mixes "is this really from Slack" with "does this person have standing", which are
different questions with different failure modes.

**The tenant is not taken from the interaction.** It is resolved from the run the
approval is bound to. An attacker who can post a signed payload could otherwise name
whichever tenant they are already trusted in and approve someone else's rollout - a
confused deputy built out of two individually correct checks.

Membership is per tenant and default-deny, which is the primary key's doing rather than
a branch someone has to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agentstack.storage.database import Database

_COLUMNS = "tenant, slack_user_id, principal, added_at, added_by"


@dataclass(frozen=True, slots=True)
class ApprovalReply:
    """A human's answer, bound to what was asked.

    It lives here rather than in `agentstack.interfaces`, where it is parsed, because
    the runtime has to consume one and contract 4 says nothing imports the channel
    layer - the same reason `TriggerEvent` moved at T9.

    `slack_user_id` is a **claim**: the adapter read it off a payload and nothing has
    checked whether this person may approve anything. `authorize_approver` below is
    what turns it into a principal.

    Note what is absent: a tenant. The tenant comes from the run this is bound to, and
    a field here would be something to be tempted by.
    """

    run_id: str
    wait_id: str
    experiment_version: str
    data_as_of: str
    approved: bool
    slack_user_id: str
    channel: str


class ApproverNotAuthorized(PermissionError):
    """This person has no standing to approve this tenant's actions."""


@dataclass(frozen=True, slots=True)
class Approver:
    tenant: str
    slack_user_id: str
    principal: str
    added_at: datetime
    added_by: str

    @staticmethod
    def of(row: tuple[Any, ...]) -> Approver:
        tenant, slack_user_id, principal, added_at, added_by = row
        return Approver(
            tenant=str(tenant),
            slack_user_id=str(slack_user_id),
            principal=str(principal),
            added_at=added_at,
            added_by=str(added_by),
        )


@dataclass(frozen=True, slots=True)
class ApproverDirectory:
    db: Database

    def add(self, *, tenant: str, slack_user_id: str, principal: str, added_by: str) -> Approver:
        """Grant standing. `added_by` is required: a group nobody can be shown to have
        changed is a group that can change without anyone noticing."""
        row = self.db.fetch_one(
            "INSERT INTO approvers (tenant, slack_user_id, principal, added_by)"
            f" VALUES (%s, %s, %s, %s) RETURNING {_COLUMNS}",
            (tenant, slack_user_id, principal, added_by),
        )
        assert row is not None  # RETURNING on a successful insert always yields a row
        return Approver.of(row)

    def remove(self, *, tenant: str, slack_user_id: str) -> bool:
        rows = self.db.fetch_all(
            "DELETE FROM approvers WHERE tenant = %s AND slack_user_id = %s RETURNING tenant",
            (tenant, slack_user_id),
        )
        return bool(rows)

    def find(self, *, tenant: str, slack_user_id: str) -> Approver | None:
        row = self.db.fetch_one(
            f"SELECT {_COLUMNS} FROM approvers WHERE tenant = %s AND slack_user_id = %s",
            (tenant, slack_user_id),
        )
        return None if row is None else Approver.of(row)

    def for_tenant(self, tenant: str) -> tuple[Approver, ...]:
        rows = self.db.fetch_all(
            f"SELECT {_COLUMNS} FROM approvers WHERE tenant = %s ORDER BY principal", (tenant,)
        )
        return tuple(Approver.of(row) for row in rows)


def authorize_approver(
    directory: ApproverDirectory, *, tenant: str, claimed_user_id: str
) -> Approver:
    """Turn a claim into a principal, or refuse.

    `tenant` must come from the run this approval is bound to, never from the payload
    the claim arrived in. The argument is named for the run's tenant precisely so that
    passing the interaction's own idea of it looks wrong at the call site.
    """
    if not claimed_user_id.strip():
        raise ApproverNotAuthorized("the interaction names no user, so nobody approved it")
    approver = directory.find(tenant=tenant, slack_user_id=claimed_user_id)
    if approver is None:
        raise ApproverNotAuthorized(
            f"{claimed_user_id} is not an approver for {tenant}. A correctly signed "
            "interaction proves where it came from, not that the person who sent it "
            "has standing here."
        )
    return approver
