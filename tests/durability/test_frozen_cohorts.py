"""T40b: the frozen cohort a proposal rests on, recorded when its cycle settles.

What the rollout carries (the cohort predicate) and what the approver is shown (how
many customers, how much revenue at risk) used to live only in the process that scored
the cohort. Now it is written down once, at the one moment that is safe: after layer 8
authorised the proposal, before the cycle settles.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.context.targeting import Cohort
from agentstack.interfaces.triggers import parse_trigger
from agentstack.policy.triggers import Outcome, OutcomeNotAuthorized, TriggerEvent
from agentstack.runtime.cycles import CycleStore, evaluate_to_settled
from agentstack.runtime.operator import evaluate_trigger
from agentstack.runtime.temporal.contracts import Trigger
from agentstack.storage.database import Database, IntegrityViolation
from tests.fitness.test_trigger_to_candidate import RULE, WATERMARK, StubScorer
from tests.temporal_support import activities_for

pytestmark = pytest.mark.usefixtures("fixture_dataset")

TENANT = "acme"


def trigger(kind: str = "data_arrival") -> Trigger:
    return Trigger(kind=kind, experiment_id="exp-7", data_as_of=WATERMARK, tenant=TENANT)


def frozen_rows(db: Database) -> int:
    row = db.fetch_one("SELECT count(*) FROM frozen_cohorts")
    assert row is not None
    return int(row[0])


def test_a_proposal_records_the_cohort_it_froze(app_database: Database) -> None:
    result = activities_for(app_database, scorer=StubScorer(), rule=RULE).evaluate_cycle(trigger())

    assert result.experiment_version is not None
    frozen = FrozenCohortStore(db=app_database).get(
        tenant=TENANT, experiment_id="exp-7", experiment_version=result.experiment_version
    )
    assert frozen is not None
    assert frozen.size == 40
    assert frozen.targeting_model_version == "stub-1"
    assert frozen.annual_value_at_risk_cents > 0
    assert frozen.data_as_of == WATERMARK
    assert frozen.description["experiment_version"] == result.experiment_version


def test_a_refused_proposal_records_no_cohort(app_database: Database) -> None:
    """Recorded after layer 8 authorises, so a proposal it refused leaves nothing."""
    activities = activities_for(app_database, scorer=StubScorer(), rule=RULE)

    with pytest.raises(OutcomeNotAuthorized):
        activities.evaluate_cycle(trigger(kind="metric_movement"))

    assert frozen_rows(app_database) == 0


def test_an_abstention_records_no_cohort(app_database: Database) -> None:
    activities = activities_for(
        app_database, scorer=StubScorer(), rule=replace(RULE, minimum_cohort=10_000)
    )

    assert activities.evaluate_cycle(trigger()).outcome == "abstain"
    assert frozen_rows(app_database) == 0


def test_a_death_between_recording_and_settling_converges(app_database: Database) -> None:
    """Recorded, then the process dies before the cycle settles. The rerun evaluates
    again (the claim is unsettled), records the same cohort again (one row: it is keyed
    by version), and settles. The other order could lose the cohort for good."""
    store = CycleStore(db=app_database)
    cohorts = FrozenCohortStore(db=app_database)
    event = parse_trigger(
        {
            "kind": "data_arrival",
            "experiment_id": "exp-7",
            "data_as_of": WATERMARK,
            "tenant": TENANT,
        },
        source="test",
    )
    frozen: list[Cohort] = []

    def evaluator(e: TriggerEvent) -> tuple[Outcome, str | None]:
        evaluation = evaluate_trigger(e, scorer=StubScorer(), rule=RULE)
        assert evaluation.cohort is not None
        frozen.append(evaluation.cohort)
        return evaluation.as_seam_result()

    def record(outcome: Outcome, version: str | None) -> None:
        # Handed the authorised outcome and the version it names, before anything settles.
        assert outcome is Outcome.PROPOSE and version == frozen[-1].experiment_version
        cohorts.record(tenant=TENANT, experiment_id="exp-7", cohort=frozen[-1])

    def record_then_die(outcome: Outcome, version: str | None) -> None:
        record(outcome, version)
        raise RuntimeError("the process died after recording the cohort")

    with pytest.raises(RuntimeError, match="after recording"):
        evaluate_to_settled(store, event, evaluator, before_settle=record_then_die)
    assert frozen_rows(app_database) == 1
    assert len(store.unsettled()) == 1

    settled = evaluate_to_settled(store, event, evaluator, before_settle=record)

    assert settled.outcome is Outcome.PROPOSE
    assert frozen_rows(app_database) == 1
    assert store.unsettled() == ()


def test_a_frozen_cohort_cannot_be_edited(app_database: Database) -> None:
    """An approver saw it. Editing it afterwards would make the approval about something else."""
    activities_for(app_database, scorer=StubScorer(), rule=RULE).evaluate_cycle(trigger())

    with pytest.raises(IntegrityViolation) as refused:
        app_database.execute("UPDATE frozen_cohorts SET size = size + 1")
    assert refused.value.constraint == "frozen_cohorts_are_frozen"
    with pytest.raises(IntegrityViolation):
        app_database.execute("DELETE FROM frozen_cohorts")
