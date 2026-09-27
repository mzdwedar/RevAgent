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
from agentstack.policy.approvers import ApprovalReply, ApproverNotAuthorized
from agentstack.runtime.approvals import Resolution
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
