"""T42: a person's answer in Slack reaches the run, and only after layer 8 has ruled on it.

`wiring.answer` is the path: the request is proven Slack's, fresh and first (layer 1);
the person is authorised against the run's own tenant and the answer recorded against
the wait (layer 8); and only then is the workflow woken, by a signal that carries ids
and grants nothing (ADR-0008 rule 4). Refused anywhere on the way, the run hears nothing.

Against the time-skipping server: the run drafts, proposes the rollout, parks, and asks.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import pytest

from agentstack.interfaces.slack_callback import expected_signature
from agentstack.interfaces.wiring import Stack, answer
from agentstack.observability.audit import AuditRecord
from agentstack.policy.approvers import ApprovalReply, ApproverNotAuthorized
from agentstack.runtime.approvals import ReplyNotApplicable, Resolution
from agentstack.runtime.temporal.contracts import REASK_EVERY, RunProgress
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.runtime.waits import WaitStore
from agentstack.storage.database import Database
from tests.durability.test_approval_wait import PAYLOAD, posted, propose_and_wait
from tests.durability.test_approval_wait import stack as stack  # the same fixture
from tests.temporal_support import progress_until

pytestmark = pytest.mark.usefixtures("fixture_dataset")

SECRET = "t42-signing-secret-for-tests-only"
APPROVER = "UANA"


@pytest.fixture(autouse=True)
def _approver(stack: Stack) -> None:
    stack.approver_directory.add(
        tenant=PAYLOAD["tenant"], slack_user_id=APPROVER, principal="ana@acme", added_by="t42"
    )


def slack_click(parked: RunProgress, *, user: str = APPROVER, approve: bool = True) -> Any:
    """The interaction Slack sends when someone clicks, signed as Slack signs it."""
    cycle = parked.cycles[0]
    # What the question carried: the run, the wait and the frozen cohort it's about.
    binding = "|".join(
        (
            parked.run_id,
            parked.awaiting_approval or "",
            cycle.experiment_version or "",
            cycle.data_as_of,
        )
    )
    payload = {
        "type": "block_actions",
        "user": {"id": user, "name": "someone"},
        "channel": {"id": "C1"},
        "actions": [
            {
                "action_id": "approve_rollout" if approve else "refuse_rollout",
                "value": binding,
                "type": "button",
            }
        ],
    }
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    sent_at = str(int(time.time()))
    return {
        "raw_body": raw,
        "sent_at": sent_at,
        "signature": expected_signature(SECRET, sent_at=sent_at, raw_body=raw),
        "secret": SECRET,
    }


def approvals(db: Database) -> int:
    """Approvals a person gave. The draft's own grant is a policy rule's (PRE_COMMIT)."""
    row = db.fetch_one("SELECT count(*) FROM approvals WHERE granted_by = 'human'")
    assert row is not None
    return int(row[0])


def test_an_approvers_answer_is_recorded_and_then_wakes_the_run(
    stack: Stack, app_database: Database
) -> None:
    resolutions: list[Resolution] = []

    async def click(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        resolutions.append(await answer(stack, env.client, **slack_click(parked)))
        return await progress_until(handle, lambda p: p.awaiting_approval is None)

    _, parked, woken = propose_and_wait(stack, app_database, then=click)

    (resolution,) = resolutions
    assert resolution.approved and resolution.approval_id is not None
    assert resolution.approver == "ana@acme"
    assert woken.answered == (parked.awaiting_approval,)
    wait = WaitStore(db=app_database).get(parked.awaiting_approval or "")
    assert wait is not None and wait.satisfied
    assert wait.payload["approved_by"] == "ana@acme"
    assert approvals(app_database) == 1


def test_an_outsiders_answer_changes_nothing(stack: Stack, app_database: Database) -> None:
    """Criterion 39: refused by layer 8 before anything is recorded or signalled."""

    async def outsider(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        with pytest.raises(ApproverNotAuthorized):
            await answer(stack, env.client, **slack_click(parked, user="UEVE"))
        after: RunProgress = await handle.query(ExperimentWorkflow.progress)
        return after

    _, parked, after = propose_and_wait(stack, app_database, then=outsider)

    assert after.answered == ()
    assert after.awaiting_approval == parked.awaiting_approval, "still waiting for a real answer"
    wait = WaitStore(db=app_database).get(parked.awaiting_approval or "")
    assert wait is not None and not wait.satisfied
    assert approvals(app_database) == 0


def test_a_refusal_is_an_answer_too(stack: Stack, app_database: Database) -> None:
    """Recorded as a refusal, and the run is woken to act on it: nothing is granted."""
    resolutions: list[Resolution] = []

    async def refuse(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        resolutions.append(await answer(stack, env.client, **slack_click(parked, approve=False)))
        return await progress_until(handle, lambda p: p.awaiting_approval is None)

    _, parked, woken = propose_and_wait(stack, app_database, then=refuse)

    (resolution,) = resolutions
    assert not resolution.approved and resolution.approval_id is None
    assert woken.answered == (parked.awaiting_approval,)
    assert approvals(app_database) == 0


def answer_records(stack: Stack, run_id: str) -> list[AuditRecord]:
    return [r for r in stack.audit.for_run(run_id) if r.surface == "approval"]


@pytest.mark.parametrize(
    ("user", "approve", "principal", "decision", "outcome"),
    [
        (APPROVER, True, "ana@acme", "human.approved", "approved"),
        (APPROVER, False, "ana@acme", "human.refused", "refused"),
        ("UEVE", True, "slack:UEVE", "approver.not_authorized", "refused"),
    ],
    ids=["a-yes", "a-no", "an-outsider"],
)
def test_every_answer_is_an_accountability_record_that_outlives_the_session(
    stack: Stack,
    app_database: Database,
    user: str,
    approve: bool,
    principal: str,
    decision: str,
    outcome: str,
) -> None:
    """A1, H3. A "no" used to live only in `waits.payload`, which goes with the session,
    and the audit trail held only the gateway's refusal under the agent's name: a
    considered "no", a forged wake-up and an outsider's click left the same record. Each
    answer is now its own record in the audit schema, naming who answered (as a principal,
    or as a bare claim for an outsider), the wait, the fingerprint and the snapshot, and
    deleting the session leaves it standing."""

    async def click(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        try:
            await answer(stack, env.client, **slack_click(parked, user=user, approve=approve))
        except ApproverNotAuthorized:
            progress: RunProgress = await handle.query(ExperimentWorkflow.progress)
            return progress
        # Answered: let the run act on it before the environment closes under it.
        return await progress_until(handle, lambda p: len(p.commits) == 1)

    run_id, parked, _ = propose_and_wait(stack, app_database, then=click)

    wait = WaitStore(db=app_database).get(parked.awaiting_approval or "")
    assert wait is not None
    (record,) = answer_records(stack, run_id)
    assert (record.principal, record.policy_decision, record.outcome) == (
        principal,
        decision,
        outcome,
    )
    assert record.tenant == "acme" and record.wait_id == wait.wait_id
    assert (record.action_fingerprint, record.state_snapshot) == (
        wait.action_fingerprint,
        wait.state_snapshot,
    )
    assert (record.approval_id is not None) == (decision == "human.approved")

    run = stack.runs.get(run_id)
    assert run is not None
    app_database.execute("DELETE FROM sessions WHERE session_id = %s", (run.session_id,))
    assert WaitStore(db=app_database).get(wait.wait_id) is None, "the operational record went"
    assert answer_records(stack, run_id) == [record], "the accountability record did not"


def test_an_answer_to_a_question_already_answered_is_on_the_record_too(
    stack: Stack, app_database: Database
) -> None:
    """The second click on one question is refused, not a second decision, and the
    attempt is recorded under the person who made it."""

    async def twice(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await answer(stack, env.client, **slack_click(parked))
        with pytest.raises(ReplyNotApplicable, match="already answered"):
            await answer(stack, env.client, **slack_click(parked, approve=False))
        progress: RunProgress = await handle.query(ExperimentWorkflow.progress)
        return progress

    run_id, _, _ = propose_and_wait(stack, app_database, then=twice)

    decided = [(r.policy_decision, r.outcome) for r in answer_records(stack, run_id)]
    assert decided == [("human.approved", "approved"), ("answer.not_applicable", "refused")]


def test_an_answer_whose_signal_was_lost_is_found_at_the_next_ask(
    stack: Stack, app_database: Database
) -> None:
    """Recorded by layer 8, never signalled (the callback died, or Temporal was down).
    The next ask finds the wait answered and says so: one interval late, not lost."""

    async def record_without_waking(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        cycle = parked.cycles[0]
        stack.coordinator.apply(
            ApprovalReply(
                run_id=parked.run_id,
                wait_id=parked.awaiting_approval or "",
                experiment_version=cycle.experiment_version or "",
                data_as_of=cycle.data_as_of,
                approved=True,
                slack_user_id=APPROVER,
                channel="C1",
            )
        )
        await env.sleep(REASK_EVERY + timedelta(minutes=1))
        return await progress_until(handle, lambda p: p.awaiting_approval is None)

    _, parked, woken = propose_and_wait(stack, app_database, then=record_without_waking)

    assert woken.answered == (parked.awaiting_approval,)
    assert len(posted(stack)) == 1, "found answered at the next ask, so not put again"
