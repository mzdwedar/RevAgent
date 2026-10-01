"""Authority to roll out is two independent things, and this file holds the first.

A turn's envelope is minted from the run's stage, so only a run *in the rollout stage*
carries `experiments:rollout`; drafting and evaluating never hold it, whatever they are
shown or asked for. The second thing - an approval bound to the exact payload - is held
by `test_approval_boundary` and `test_approve_resume_rollout`. Neither substitutes for
the other: the scope says this run may reach the act, the approval says this act, as
shown, was agreed to.

(SPEC.md once said "approval mints an envelope that has the scope". It does not: the
envelope comes from the stage, and approval is checked at the gateway. This is the test
that was named for the wrong claim.)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentstack.interfaces.wiring import STAGE_SCOPES, ExperimentTurns, Stack
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.runtime.run import new_run
from agentstack.tools.experiments import (
    DRAFT_STAGE,
    EVALUATION_STAGE,
    EXPERIMENT_STAGES,
    ROLLOUT_STAGE,
)

from .conftest import TENANT, USER

ROLLOUT_SCOPE = "experiments:rollout"


def _envelope(stack: Stack, session_id: str, stage: str) -> IdentityEnvelope:
    run = new_run(
        session_id=session_id,
        tenant=TENANT,
        user=USER,
        stage=stage,
        channel="test",
        subject="exp-7",
    )
    return ExperimentTurns(stack).envelope(run)


def test_each_stage_is_given_exactly_its_own_scopes() -> None:
    expected = {
        DRAFT_STAGE: frozenset({"experiments:draft"}),
        EVALUATION_STAGE: frozenset({"experiments:annotate", "experiments:halt"}),
        ROLLOUT_STAGE: frozenset({ROLLOUT_SCOPE}),
    }
    assert dict(STAGE_SCOPES) == expected


def test_every_experiment_stage_has_an_entry() -> None:
    """A stage added to the experiment set without a scope row would mint an envelope
    with no scopes - which the envelope refuses - so a new stage fails loudly here
    rather than at a customer's first run."""
    assert set(STAGE_SCOPES) == EXPERIMENT_STAGES


@pytest.mark.parametrize("stage", sorted(EXPERIMENT_STAGES))
def test_only_a_run_in_the_rollout_stage_carries_the_rollout_scope(
    stack: Stack, session_id: str, stage: str
) -> None:
    envelope = _envelope(stack, session_id, stage)

    assert (ROLLOUT_SCOPE in envelope.delegation_scopes) is (stage == ROLLOUT_STAGE)


def test_a_stage_nobody_defined_holds_no_authority_at_all(stack: Stack, session_id: str) -> None:
    """Least privilege by absence: no row means no scopes, and an envelope with no
    scopes cannot be constructed - so the turn cannot start, rather than start broad."""
    with pytest.raises(ValueError, match="no delegation scopes"):
        _envelope(stack, session_id, "no-such-stage")


def test_the_envelope_is_short_lived_and_revocable(stack: Stack, session_id: str) -> None:
    envelope = _envelope(stack, session_id, ROLLOUT_STAGE)

    assert envelope.revocable
    assert envelope.expires_at - datetime.now(UTC) <= timedelta(minutes=15)
