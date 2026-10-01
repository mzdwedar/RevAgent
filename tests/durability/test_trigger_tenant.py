"""A trigger's tenant is a claim, and layer 8 tests it against the run (audit M2).

`subject_of` used to be the only place the claim was tested, and nothing in production
called it. The cycle is the first thing that acts on a trigger, so it is where the
claim meets the run: the run's tenant comes from its own record, never from the payload
that arrived at it.
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

from agentstack.policy.triggers import TenantClaimRefused, TriggerEvent, TriggerKind
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.temporal.contracts import Trigger
from agentstack.runtime.temporal.retry import REFUSALS
from agentstack.storage.database import Database
from tests.fitness.test_trigger_to_candidate import RULE, WATERMARK, StubScorer
from tests.temporal_support import activities_for

pytestmark = pytest.mark.usefixtures("fixture_dataset")


def _trigger(tenant: str) -> Trigger:
    return Trigger(
        kind="data_arrival",
        experiment_id="exp-7",
        data_as_of=WATERMARK,
        tenant=tenant,
        source="test",
    )


def test_a_trigger_for_another_tenant_cannot_run_a_cycle(
    app_database: Database, run_id: str
) -> None:
    scorer = StubScorer()
    activities = activities_for(app_database, scorer=scorer, rule=RULE)

    with pytest.raises(ApplicationError) as refused:
        activities.evaluate_cycle(_trigger("globex"), run_id)

    assert refused.value.type == TenantClaimRefused.__name__
    assert refused.value.non_retryable
    assert scorer.calls == 0, "the cohort was scored for a tenant the run does not act for"
    assert CycleStore(db=app_database).unsettled() == ()
    assert app_database.fetch_one("SELECT count(*) FROM trigger_cycles") == (0,)


def test_the_refusal_is_audited_under_the_runs_tenant(app_database: Database, run_id: str) -> None:

    with pytest.raises(ApplicationError):
        activities_for(app_database, scorer=StubScorer(), rule=RULE).evaluate_cycle(
            _trigger("globex"), run_id
        )

    row = app_database.fetch_one(
        "SELECT tenant, policy_decision, outcome FROM audit.records WHERE run_id = %s",
        (run_id,),
    )
    assert row == ("acme", "trigger.tenant_refused", "refused")


def test_a_trigger_for_the_runs_own_tenant_still_runs(app_database: Database, run_id: str) -> None:

    result = activities_for(app_database, scorer=StubScorer(), rule=RULE).evaluate_cycle(
        _trigger("acme"), run_id
    )

    assert result.outcome == "propose"


def test_a_claim_is_refused_in_layer_8_by_name() -> None:
    from agentstack.policy.triggers import authorize_trigger_tenant

    event = TriggerEvent(
        kind=TriggerKind.DATA_ARRIVAL, experiment_id="exp-7", data_as_of="x", tenant="globex"
    )
    with pytest.raises(TenantClaimRefused):
        authorize_trigger_tenant(event, run_tenant="acme")
    assert authorize_trigger_tenant(event, run_tenant="globex") is event


def test_the_refusal_is_never_retried() -> None:
    assert TenantClaimRefused.__name__ in REFUSALS
