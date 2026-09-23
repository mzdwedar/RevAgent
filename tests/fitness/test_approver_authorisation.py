"""Criterion 25: an unauthorised approver is refused in layer 8, not in the adapter.

A correctly signed interaction proves where a payload came from. It says nothing about
whether the person who sent it has standing, and those are different questions with
different failure modes. Deciding the second one at the transport boundary would put
the authorisation in the layer least able to audit it.

The load-bearing detail is where the *tenant* comes from: the run the approval is bound
to, never the payload the claim arrived in. Two individually correct checks - "this is
really from Slack" and "this person is an approver" - compose into a confused deputy if
the attacker gets to name the tenant they are checked against.
"""

from __future__ import annotations

import inspect
import json
import time
from urllib.parse import urlencode

import pytest

from agentstack.interfaces import slack_callback
from agentstack.interfaces.slack_callback import ReplayGuard, accept, expected_signature
from agentstack.interfaces.wiring import Stack
from agentstack.policy import approvers as approvers_module
from agentstack.policy.approvers import (
    ApproverDirectory,
    ApproverNotAuthorized,
    authorize_approver,
)
from agentstack.storage.database import Database, IntegrityViolation

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


@pytest.fixture
def directory(app_database: Database) -> ApproverDirectory:
    return ApproverDirectory(db=app_database)


def signed_reply(user: str, binding: str = "run-7|wait-1|exp:1|d:1") -> tuple[bytes, str, str]:
    payload = {
        "type": "block_actions",
        "user": {"id": user, "name": "somebody"},
        "channel": {"id": "C1"},
        "actions": [{"action_id": "approve_rollout", "value": binding, "type": "button"}],
    }
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    sent_at = str(int(time.time()))
    return raw, sent_at, expected_signature(SECRET, sent_at=sent_at, raw_body=raw)


# --- the criterion, stated as the path it takes ---


def test_a_correctly_signed_outsider_gets_past_the_adapter_and_is_refused_by_policy(
    directory: ApproverDirectory, app_database: Database
) -> None:
    """The whole of criterion 25 in one test.

    The interaction is genuine: right secret, fresh, not replayed. The adapter accepts
    it, as it should - it has no opinion about who may approve. Layer 8 refuses it.
    """
    raw, sent_at, signature = signed_reply("UOUTSIDER")
    guard = ReplayGuard(db=app_database)

    reply = accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)
    assert reply.slack_user_id == "UOUTSIDER", "the adapter passed the claim on untouched"

    with pytest.raises(ApproverNotAuthorized, match="not an approver"):
        authorize_approver(directory, tenant="acme", claimed_user_id=reply.slack_user_id)


def test_the_refusal_says_what_a_signature_does_and_does_not_prove(
    directory: ApproverDirectory,
) -> None:
    with pytest.raises(ApproverNotAuthorized) as caught:
        authorize_approver(directory, tenant="acme", claimed_user_id="UNOBODY")

    assert "not that the person who sent it has standing" in str(caught.value)


def test_a_member_is_authorised_and_becomes_a_principal(
    directory: ApproverDirectory,
) -> None:
    """The counterpart: the refusals must not pass because nothing is ever authorised."""
    directory.add(tenant="acme", slack_user_id="UANA", principal="ana@acme.example", added_by="ops")

    approver = authorize_approver(directory, tenant="acme", claimed_user_id="UANA")

    assert approver.principal == "ana@acme.example"


# --- tenant scoping ---


def test_one_tenants_approver_has_no_standing_over_another(
    directory: ApproverDirectory,
) -> None:
    """Tenant A's rollout is not approvable by tenant B's people."""
    directory.add(tenant="acme", slack_user_id="UANA", principal="ana", added_by="ops")

    assert authorize_approver(directory, tenant="acme", claimed_user_id="UANA")

    with pytest.raises(ApproverNotAuthorized, match="not an approver for other"):
        authorize_approver(directory, tenant="other", claimed_user_id="UANA")


def test_standing_in_two_tenants_takes_two_rows(directory: ApproverDirectory) -> None:
    """Default deny is the primary key's doing, not a branch someone remembers."""
    directory.add(tenant="acme", slack_user_id="UANA", principal="ana", added_by="ops")
    directory.add(tenant="other", slack_user_id="UANA", principal="ana", added_by="ops")

    assert authorize_approver(directory, tenant="other", claimed_user_id="UANA").principal == "ana"


def test_the_tenant_comes_from_the_run_not_from_the_payload() -> None:
    """The confused deputy this design is avoiding.

    Two individually correct checks - "this is really from Slack" and "this person is
    an approver" - compose into a hole if the attacker names the tenant they are
    checked against. The parameter is named for the run's tenant so that passing the
    interaction's own idea of it looks wrong where it is written.
    """
    signature = inspect.signature(authorize_approver)

    assert "tenant" in signature.parameters
    assert "claimed_user_id" in signature.parameters
    doc = authorize_approver.__doc__ or ""
    assert "never from the payload" in doc

    # And the reply the adapter produces carries no tenant to be tempted by.
    assert "tenant" not in slack_callback.ApprovalReply.__dataclass_fields__


# --- default deny, and the shape of the record ---


def test_an_empty_claim_is_refused(directory: ApproverDirectory) -> None:
    with pytest.raises(ApproverNotAuthorized, match="nobody approved it"):
        authorize_approver(directory, tenant="acme", claimed_user_id="   ")


def test_removing_standing_takes_effect(directory: ApproverDirectory) -> None:
    directory.add(tenant="acme", slack_user_id="UANA", principal="ana", added_by="ops")

    assert directory.remove(tenant="acme", slack_user_id="UANA") is True

    with pytest.raises(ApproverNotAuthorized):
        authorize_approver(directory, tenant="acme", claimed_user_id="UANA")


def test_removing_someone_who_was_never_there_says_so(directory: ApproverDirectory) -> None:
    assert directory.remove(tenant="acme", slack_user_id="UGHOST") is False


def test_the_group_records_who_changed_it(directory: ApproverDirectory) -> None:
    """A group nobody can be shown to have changed is one that can change unnoticed."""
    approver = directory.add(
        tenant="acme", slack_user_id="UANA", principal="ana", added_by="ops-oncall"
    )

    assert approver.added_by == "ops-oncall"
    assert directory.for_tenant("acme")[0].added_by == "ops-oncall"


def test_the_database_refuses_an_approver_with_no_principal(
    app_database: Database,
) -> None:
    """The audit trail records the principal, so an approver without one is unauditable."""
    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(
            "INSERT INTO approvers (tenant, slack_user_id, principal, added_by)"
            " VALUES ('acme', 'U1', '  ', 'ops')"
        )

    assert caught.value.constraint == "approver_names_a_principal"


def test_the_database_refuses_an_approver_nobody_added(app_database: Database) -> None:
    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(
            "INSERT INTO approvers (tenant, slack_user_id, principal, added_by)"
            " VALUES ('acme', 'U1', 'ana', '')"
        )

    assert caught.value.constraint == "approver_records_who_added_them"


def test_the_principal_survives_a_recycled_slack_id(directory: ApproverDirectory) -> None:
    """ "Who approved this" must stay answerable after someone leaves and their id is
    handed to a new hire."""
    directory.add(tenant="acme", slack_user_id="U1", principal="ana@acme", added_by="ops")
    directory.remove(tenant="acme", slack_user_id="U1")
    directory.add(tenant="acme", slack_user_id="U1", principal="ben@acme", added_by="ops")

    assert (
        authorize_approver(directory, tenant="acme", claimed_user_id="U1").principal == "ben@acme"
    )


# --- the layer boundary itself ---


def test_the_adapter_does_not_authorise_anyone() -> None:
    """Criterion 25 is about *where* the decision is made.

    The adapter names `policy.approvers` because `ApprovalReply` lives there - contract
    4 forbids the runtime importing the channel layer, so the reply had to move down.
    What it must not do is consult the directory or call the authorisation, which is
    what this checks rather than the mere mention of a module name.
    """
    source = inspect.getsource(slack_callback)

    assert "ApproverDirectory" not in source
    assert "authorize_approver" not in source
    assert "ApprovalReply" in source, "it still builds one; it just does not judge it"


def test_the_directory_knows_nothing_about_slack_signatures() -> None:
    """The other direction: layer 8 does not re-check the transport."""
    source = inspect.getsource(approvers_module)

    for transport in ("hmac", "signature", "timestamp", "replay"):
        assert transport not in source.lower()


def test_the_stack_exposes_the_directory_for_the_callback_path(stack: Stack) -> None:
    assert isinstance(stack.approver_directory, ApproverDirectory)
