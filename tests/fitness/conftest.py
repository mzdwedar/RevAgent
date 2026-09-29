"""Shared builders for the architecture fitness suite.

These tests assert the invariants in STACK.md. They are not unit tests of behavior:
each one fails when a layer boundary has been collapsed, which is the only thing that
makes the boundary real.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from agentstack.execution.surfaces import RegistryClient, SurfaceRefused
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.runtime.run import Run, new_run
from agentstack.storage.database import Database
from agentstack.tools.experiments import ROLLOUT_STAGE
from agentstack.tools.spec import Surface

TENANT = "acme"
USER = "agent-operator"
SCOPES = frozenset({"experiments:rollout"})
READ_SCOPES = frozenset({"experiments:read"})
# The irreversible act every approval, idempotency and identity test drives: a rollout
# to 10% of one frozen cohort. `ROLLOUT_ARGS` is the validated form of the message.
ROLLOUT_ARGS = {
    "tenant": TENANT,
    "experiment_id": "exp-7",
    "experiment_version": "exp:5cbf2762",
    "percentage": 10,
    "targeting_model_version": "tabpfn-3.5",
    "risk_threshold": 0.61,
    "prior_rollout_event": 0,
}
ROLLOUT_MESSAGE = (
    "roll_out_variant_to_percentage tenant=acme experiment_id=exp-7 "
    "experiment_version=exp:5cbf2762 percentage=10 targeting_model_version=tabpfn-3.5 "
    "risk_threshold=0.61 prior_rollout_event=0"
)


def seed_experiment(stack: Stack, experiment_id: str = "exp-7") -> None:
    """What an earlier drafting turn wrote, on whichever registry the gateway acts on.

    The registry refuses to roll out an experiment that was never drafted, or at a
    version that is not its current one. Idempotent, so a test that swaps the surface
    and one that asks for the message can do it in either order."""
    surface = stack.deps.gateway.surfaces[Surface.REGISTRY]
    resource = f"{TENANT}/experiments/{experiment_id}"
    if surface.state(resource) is None:
        surface.commit(
            resource,
            {
                "experiment_version": ROLLOUT_ARGS["experiment_version"],
                "hypothesis": "a discount retains at-risk customers",
                "variant": "20-percent-off",
            },
        )


@dataclass
class FlakyRegistry(RegistryClient):
    """The in-memory registry with the two ways a surface lets you down.

    `fail_after_effect` applies the effect and loses the answer; `refuse_before_effect`
    is a precondition that did not hold, so nothing applied. A surface that cannot
    produce either makes every test about idempotency vacuous."""

    refuse_before_effect: bool = False

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        if self.refuse_before_effect:
            raise SurfaceRefused(f"{resource}: refused, nothing applied")
        return super().commit(resource, payload)


@pytest.fixture
def flaky_registry(stack: Stack) -> FlakyRegistry:
    """The gateway acting on a registry that can be told to misbehave."""
    registry = FlakyRegistry()
    stack.deps.gateway.surfaces[Surface.REGISTRY] = registry
    seed_experiment(stack)
    return registry


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Iterator[Stack]:
    yield build_stack(app_database, checkpointer, tenant=TENANT)


@pytest.fixture
def session_id(stack: Stack) -> str:
    return stack.resolver.start(user_id=USER, tenant=TENANT).session_id


@pytest.fixture
def event(stack: Stack, session_id: str) -> InboundEvent:
    # The message names an experiment, and the registry refuses to roll out one that
    # was never drafted, so whoever asks for the rollout has drafted it.
    seed_experiment(stack)
    return InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=session_id,
        text=ROLLOUT_MESSAGE,
    )


@pytest.fixture
def run(stack: Stack, session_id: str) -> Run:
    """A persisted run. Steps and waits carry a foreign key onto it."""
    return stack.runs.ensure(
        new_run(
            session_id=session_id, tenant=TENANT, user=USER, stage=ROLLOUT_STAGE, channel="test"
        )
    )


def fresh_run(stack: Stack, session_id: str) -> Run:
    """A second, distinct run on the rollout stage: same session, new run id."""
    return stack.runs.ensure(
        new_run(
            session_id=session_id, tenant=TENANT, user=USER, stage=ROLLOUT_STAGE, channel="test"
        )
    )


def approve_and_resume(stack: Stack, result, run: Run, approver: str = "finance-oncall") -> None:
    """Grant the approval the run is parked on, then satisfy the wait.

    Deliberately explicit: the approval binds to this run, this action fingerprint and
    the state snapshot the approver was shown.
    """
    from agentstack.runtime.waits import ResumeEvent, resume

    wait = result.pending_wait
    request = result.pending_request
    assert wait is not None and request is not None, "expected the run to be awaiting approval"
    assert result.approval_summary, "an awaiting run must carry what the approver will see"
    stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot=wait.state_snapshot,
        approver=approver,
        summary=result.approval_summary,
    )
    resume(
        stack.waits,
        ResumeEvent(
            run_id=run.run_id,
            wait_id=wait.wait_id,
            state_snapshot=wait.state_snapshot,
            payload={"approved_by": approver},
        ),
    )


def drive_to_completion(
    stack: Stack,
    event: InboundEvent,
    run: Run,
    *,
    scopes: frozenset[str] = SCOPES,
    approver: str = "finance-oncall",
    max_rounds: int = 6,
):
    """Run the turn, approving whatever it parks on, until it stops parking.

    This is what an approval queue looks like from the outside: each action surfaces
    on its own, bound to its own state, and is approved on its own. A helper that
    approved everything up front would be the modal this whole design rejects.
    """
    result = handle(stack, event, scopes=scopes, run=run)
    for _ in range(max_rounds):
        if result.status != "awaiting_approval":
            return result
        approve_and_resume(stack, result, run, approver)
        result = handle(stack, event, scopes=scopes, run=run)
    raise AssertionError(f"still parked after {max_rounds} rounds")
