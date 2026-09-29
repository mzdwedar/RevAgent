"""A2 (audit M1, H5): an effect of unknown outcome parks where a person sees and settles it.

M1: the turn path met an `UnresolvedEffect` and parked nothing. The claim stayed
IN_FLIGHT, visible to `unresolved_keys()` and to nobody watching runs. Now the turn parks
a reconcile wait, the workflow waits on it, and once the claim is settled the *same* turn
resumes from its checkpoint: the model isn't asked again and the ledger answers.

H5: nothing told a person. `operator stalled` lists the wait (tests/fitness), and
`operator reconcile` settles it: the claim, an audit record naming who and why, the
wait, and a wake-up for the run through the same signal an approval uses.

The runs here are real workflows on the time-skipping server, driven by the production
worker; only the surface is swapped, to lose an answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from temporalio.client import Client

from agentstack.execution.surfaces import SurfaceTimeout
from agentstack.interfaces import operator_cli
from agentstack.interfaces.wiring import Stack, answer, deliver
from agentstack.runtime.reconcile import APPLIED, NOT_APPLIED, ReconcileRefused, settle
from agentstack.runtime.run import Run, new_run
from agentstack.runtime.temporal.client import notify_answer
from agentstack.runtime.temporal.contracts import (
    UNRESOLVED,
    CommitOutcome,
    RunProgress,
    workflow_id,
)
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.runtime.waits import RECONCILE, Wait, WaitStore, park_reconcile
from agentstack.storage.database import Database
from agentstack.tools.spec import Surface
from tests.durability.test_approval_wait import PAYLOAD, propose_and_wait
from tests.durability.test_approval_wait import stack as stack  # the same fixture
from tests.durability.test_commit import LosesTheFirstRolloutAnswer, acted
from tests.durability.test_slack_answer import APPROVER, slack_click
from tests.fitness.test_trigger_to_candidate import RULE, StubScorer
from tests.temporal_support import (
    DraftingEngine,
    asker_for,
    progress_until,
    time_skipping,
    turns_for,
    worker_on,
)

pytestmark = pytest.mark.usefixtures("fixture_dataset")

DRAFTED = "acme/experiments/exp-7"
OPERATOR = "ana@acme"


@pytest.fixture(autouse=True)
def _approver(stack: Stack) -> None:
    stack.approver_directory.add(
        tenant="acme", slack_user_id=APPROVER, principal=OPERATOR, added_by="a2"
    )


class LosesTheFirstDraftAnswer:
    """The first draft's answer is lost: applied and then unanswered, or (`applies=False`)
    never applied at all. From the gateway both look the same, which is the point."""

    def __init__(self, inner: Any, *, applies: bool = True) -> None:
        self.inner = inner
        self.applies = applies
        self.drafts = 0
        self.receipt: str | None = None

    def read(self, resource: str, query: dict[str, Any]) -> Any:
        return self.inner.read(resource, query)

    def state(self, resource: str) -> Any:
        return self.inner.state(resource)

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        if resource.endswith("/rollout"):
            return str(self.inner.commit(resource, payload))
        self.drafts += 1
        if self.drafts == 1:
            if self.applies:
                self.receipt = str(self.inner.commit(resource, payload))
            raise SurfaceTimeout(f"{resource}: the answer was lost")
        return str(self.inner.commit(resource, payload))


def reconcile(
    client: Client, db: Database, wait_id: str, *, receipt: str | None, reason: str = "checked"
) -> Awaitable[int]:
    async def to() -> Client:
        return client

    return operator_cli.reconcile_report(
        to, db, wait_id=wait_id, receipt=receipt, operator=OPERATOR, reason=reason
    )


def drive(
    stack: Stack, db: Database, then: Callable[[Any, Any, RunProgress], Awaitable[Any]]
) -> tuple[str, RunProgress, Any]:
    """Deliver a proposing trigger, wait until the run is parked on a reconcile wait from
    its drafting turn, then hand over to `then`."""

    async def go() -> tuple[str, RunProgress, Any]:
        async with (
            time_skipping() as env,
            worker_on(
                env.client,
                "reconcile",
                db,
                scorer=StubScorer(),
                rule=RULE,
                turns=turns_for(stack, ENGINE),
                asker=asker_for(stack),
            ),
        ):
            run_id = await deliver(stack, env.client, PAYLOAD, source="a2", task_queue="reconcile")
            handle = env.client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
            parked = await progress_until(handle, lambda p: p.reconciling is not None)
            return run_id, parked, await then(env, handle, parked)

    return asyncio.run(go())


ENGINE = DraftingEngine()


def draft_records(stack: Stack, run_id: str) -> list[tuple[str, str]]:
    return [
        (r.outcome, r.policy_decision)
        for r in stack.audit.for_run(run_id)
        if r.resource == DRAFTED or r.policy_decision in {APPLIED, NOT_APPLIED}
    ]


@pytest.fixture(autouse=True)
def _fresh_engine() -> None:
    ENGINE.calls, ENGINE.shown = 0, []


def test_a_lost_draft_answer_parks_the_run_until_reconciled_then_the_same_turn_goes_on(
    stack: Stack, app_database: Database
) -> None:
    surface = LosesTheFirstDraftAnswer(stack.deps.gateway.surfaces[Surface.REGISTRY])
    stack.deps.gateway.surfaces[Surface.REGISTRY] = surface
    seen: dict[str, Any] = {}

    async def settle(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        wait = WaitStore(db=app_database).get(parked.reconciling or "")
        seen["wait"] = wait
        seen["unresolved"] = stack.ledger.unresolved_keys()
        assert surface.receipt is not None
        assert wait is not None
        seen["code"] = await reconcile(
            env.client, app_database, wait.wait_id, receipt=surface.receipt
        )
        return await progress_until(handle, lambda p: p.awaiting_approval is not None)

    run_id, parked, done = drive(stack, app_database, settle)

    # Parked, not refused: the turn said which wait, and the record says which claim.
    (unresolved,) = parked.turns
    assert unresolved.status == UNRESOLVED and unresolved.wait_id == parked.reconciling
    wait: Wait = seen["wait"]
    assert wait.kind == RECONCILE and not wait.satisfied and wait.deadline is not None
    assert (wait.idempotency_key,) == seen["unresolved"]

    # Settled as applied, then the same turn resumed: the model drafted once, the
    # registry holds one draft, and the ledger answered the retake.
    assert seen["code"] == 0
    _, drafted, proposed = done.turns
    assert drafted.receipts == 1 and proposed.status == "awaiting_approval"
    assert ENGINE.drafts == 1, "the model was not asked to draft again"
    assert surface.drafts == 1 and list(stack.registry_client.drafts) == [DRAFTED]
    assert stack.ledger.unresolved_keys() == ()
    assert draft_records(stack, run_id) == [
        ("unresolved", "effect.unresolved"),
        ("reconciled", APPLIED),
        ("deduplicated", "approval.policy:pre_commit.reversible_within_tenant"),
    ]
    settled = WaitStore(db=app_database).get(wait.wait_id)
    assert settled is not None and settled.satisfied
    assert settled.payload["reconciled_by"] == f"operator:{OPERATOR}"
    assert settled.payload["applied"] is True and settled.payload["receipt"] == surface.receipt


def test_a_draft_that_never_applied_is_released_and_made_once(
    stack: Stack, app_database: Database
) -> None:
    surface = LosesTheFirstDraftAnswer(stack.deps.gateway.surfaces[Surface.REGISTRY], applies=False)
    stack.deps.gateway.surfaces[Surface.REGISTRY] = surface

    async def settle(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        assert stack.registry_client.drafts == {}, "nothing applied before the settlement"
        code = await reconcile(env.client, app_database, parked.reconciling or "", receipt=None)
        assert code == 0
        return await progress_until(handle, lambda p: p.awaiting_approval is not None)

    run_id, _, done = drive(stack, app_database, settle)

    assert done.turns[1].receipts == 1
    assert surface.drafts == 2, "released, then made once"
    assert list(stack.registry_client.drafts) == [DRAFTED]
    assert [outcome for outcome, _ in draft_records(stack, run_id)] == [
        "unresolved",
        "reconciled",
        "committed",
    ]


def test_a_wake_up_nobody_settled_anything_for_does_not_spin_the_run(
    stack: Stack, app_database: Database
) -> None:
    """The answer is spent once. Woken with the claim still unknown, the act meets the
    gateway's refusal once more and the run waits again, instead of going round and round
    on a claim nobody settled. Then a real settlement lets it through, deduplicated."""
    stack.deps.gateway.surfaces[Surface.REGISTRY] = LosesTheFirstRolloutAnswer(
        stack.deps.gateway.surfaces[Surface.REGISTRY]
    )
    seen: dict[str, Any] = {}

    async def forged_then_settled(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await answer(stack, env.client, **slack_click(parked))
        unresolved = await progress_until(handle, lambda p: p.reconciling is not None)
        wait_id = unresolved.reconciling or ""
        await notify_answer(env.client, run_id=parked.run_id, wait_id=wait_id)
        again = await acted(handle, 2)
        await asyncio.sleep(1.0)  # long enough for a spinning run to have gone round
        seen["after_the_forgery"] = await handle.query(ExperimentWorkflow.progress)
        seen["again"] = again
        (key,) = stack.ledger.unresolved_keys()
        seen["code"] = await reconcile(
            env.client, app_database, wait_id, receipt="rollout-seen-in-the-registry"
        )
        seen["key"] = key
        return await acted(handle, 3)

    _, _, done = propose_and_wait(stack, app_database, then=forged_then_settled)

    after: RunProgress = seen["after_the_forgery"]
    assert [c.status for c in after.commits] == [UNRESOLVED, UNRESOLVED]
    assert after.reconciling == after.commits[0].wait_id == after.commits[1].wait_id
    assert seen["code"] == 0
    assert done.commits[2] == CommitOutcome(status="deduplicated")
    assert done.reconciling is None
    assert len(stack.registry_client.rollouts) == 1
    assert stack.ledger.recorded(seen["key"]) == "rollout-seen-in-the-registry"


# --- the command itself, against the record ---------------------------------------------


KEY = "rollout:acme:exp-7:v1:10"


def unresolved_rollout(stack: Stack) -> tuple[Run, Wait]:
    """What the gateway and the act leave behind when a rollout's answer is lost: an
    IN_FLIGHT claim, the gateway's `unresolved` audit record, and the reconcile wait."""
    session = stack.resolver.start(user_id="agent-operator", tenant="acme")
    run = stack.runs.ensure(
        new_run(session_id=session.session_id, tenant="acme", user="agent-operator", channel="t")
    )
    claim = stack.ledger.claim(KEY)
    stack.audit.write(
        run_id=run.run_id,
        principal="agent-operator",
        tenant="acme",
        action_fingerprint="fp-rollout",
        surface="registry",
        resource="acme/experiments/exp-7/rollout",
        policy_decision="effect.unresolved",
        approval_id=None,
        outcome="unresolved",
    )
    wait = park_reconcile(
        stack.waits,
        run_id=run.run_id,
        action_fingerprint="fp-rollout",
        idempotency_key=KEY,
        claimed_at=claim.claimed_at,
        state_snapshot="world-1",
    )
    return run, wait


def cli(url: str, *argv: str, address: str) -> int:
    return operator_cli.main(
        ["--url", url, "reconcile", *argv, "--operator", OPERATOR, "--address", address]
    )


def test_the_command_settles_audits_and_satisfies_and_twice_is_once(
    stack: Stack,
    app_database_url: str,
    temporal_address: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, wait = unresolved_rollout(stack)
    argv = (wait.wait_id, "--applied", "rollout-42", "--reason", "registry shows it live")

    first = cli(app_database_url, *argv, address=temporal_address)
    second = cli(app_database_url, *argv, address=temporal_address)

    out = capsys.readouterr().out
    assert (first, second) == (0, 0)
    assert f"record   {wait.wait_id}  settled as applied  run {run.run_id}" in out
    assert f"record   {wait.wait_id}  already settled as applied" in out
    assert "no workflow for this run" in out, "a run outside Temporal takes it on its next turn"
    assert stack.ledger.recorded(KEY) == "rollout-42"
    satisfied = stack.waits.get(wait.wait_id)
    assert satisfied is not None and satisfied.satisfied
    assert satisfied.payload["reason"] == "registry shows it live"
    (record,) = [r for r in stack.audit.for_run(run.run_id) if r.outcome == "reconciled"]
    assert record.principal == f"operator:{OPERATOR}" and record.policy_decision == APPLIED
    assert (record.surface, record.resource) == ("registry", "acme/experiments/exp-7/rollout")
    assert record.action_fingerprint == "fp-rollout" and record.tenant == "acme"


def test_not_applied_releases_the_claim(
    stack: Stack, app_database_url: str, temporal_address: str
) -> None:
    run, wait = unresolved_rollout(stack)

    code = cli(
        app_database_url,
        wait.wait_id,
        "--not-applied",
        "--reason",
        "no event",
        address=temporal_address,
    )

    assert code == 0
    assert stack.ledger.inspect(KEY) is None, "released: the act may go again, once"
    (record,) = [r for r in stack.audit.for_run(run.run_id) if r.outcome == "reconciled"]
    assert record.policy_decision == NOT_APPLIED


@pytest.mark.parametrize(
    ("argv", "refusal"),
    [
        (("--not-applied", "--reason", "no event"), "already settled with 'rollout-42'"),
        (("--applied", "rollout-43", "--reason", "x"), "already settled with receipt 'rollout-42'"),
        (("--applied", "rollout-42", "--reason", " "), "says why"),
    ],
)
def test_a_settlement_that_contradicts_the_record_is_refused(
    stack: Stack,
    app_database_url: str,
    temporal_address: str,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
    refusal: str,
) -> None:
    _, wait = unresolved_rollout(stack)
    stack.ledger.finalize(KEY, "rollout-42")

    code = cli(app_database_url, wait.wait_id, *argv, address=temporal_address)

    assert code == 1
    assert refusal in capsys.readouterr().out
    assert stack.ledger.recorded(KEY) == "rollout-42", "the record was not moved"


def test_only_a_reconcile_wait_can_be_reconciled(
    stack: Stack, app_database_url: str, temporal_address: str, capsys: pytest.CaptureFixture[str]
) -> None:
    run, _ = unresolved_rollout(stack)
    trigger = stack.waits.park(
        run_id=run.run_id, kind="trigger", state_snapshot="s", timeout=timedelta(days=1)
    )

    code = cli(
        app_database_url,
        trigger.wait_id,
        "--not-applied",
        "--reason",
        "x",
        address=temporal_address,
    )

    assert code == 1
    assert "is not a reconcile wait" in capsys.readouterr().out


def test_settled_but_not_woken_says_so_and_can_be_run_again(
    stack: Stack, app_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The record is complete without the signal. A Temporal that can't be reached is
    reported, and the same command, run again, only wakes the run."""
    _, wait = unresolved_rollout(stack)

    code = cli(
        app_database_url, wait.wait_id, "--not-applied", "--reason", "x", address="localhost:1"
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "not woken" in out and "Run this command again" in out
    satisfied = stack.waits.get(wait.wait_id)
    assert satisfied is not None and satisfied.satisfied


@pytest.mark.parametrize(
    ("changes", "refusal"),
    [
        ({"operator": " "}, "names the operator"),
        ({"receipt": None, "applied": True}, "with the surface's receipt"),
        ({"receipt": "r-1", "applied": False}, "has no receipt to record"),
        ({"release_first": True}, "is already released"),
        ({"keyless": True}, "does not name the claim"),
    ],
)
def test_the_settlement_refuses_what_it_cannot_honestly_record(
    stack: Stack, changes: dict[str, Any], refusal: str
) -> None:
    _, wait = unresolved_rollout(stack)
    assert stack.ledger.inspect(KEY) is not None, "IN_FLIGHT, as the gateway left it"
    if changes.get("release_first"):
        stack.ledger.abandon(KEY)
    if changes.get("keyless"):
        # A reconcile wait from before 0019, satisfied and so allowed to name no claim.
        stack.runs.db.execute(
            "UPDATE waits SET satisfied = true, satisfied_at = now(), idempotency_key = NULL"
            " WHERE wait_id = %s",
            (wait.wait_id,),
        )

    with pytest.raises(ReconcileRefused, match=refusal):
        settle(
            runs=stack.runs,
            waits=stack.waits,
            ledger=stack.ledger,
            audit=stack.audit,
            wait_id=wait.wait_id,
            applied=changes.get("applied", True),
            receipt=changes.get("receipt", "rollout-42"),
            operator=changes.get("operator", OPERATOR),
            reason="checked",
        )
