"""The path Checkpoint C asked for: a trigger becomes a frozen cohort, or a reason.

Every piece of this existed and was tested alone - the ingress and the idempotent cycle
(T9), the cohort and its watermark (T5), the targeting freeze (T7). What no task had
built was the code joining them, so `cycles.evaluate` took its evaluator as a seam and
nothing filled it. This is the filling.

What it deliberately stops short of is drafting. Drafting is the model's job and happens
in a turn, through the exposure filter, policy and the gateway. This produces the cohort
a draft is written *about*, because the point of deterministic targeting is that no
model gets a say in who enters the experiment.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from agentstack.context.datasets import REGISTRY, CohortSnapshot, DatasetSpec
from agentstack.context.targeting import TargetingRule
from agentstack.interfaces.triggers import parse_trigger
from agentstack.policy.triggers import Outcome, TriggerEvent
from agentstack.prediction.churn import ChurnScores
from agentstack.runtime.cycles import CycleStore, evaluate
from agentstack.runtime.operator import (
    TriggerNotUnderstood,
    dataset_of,
    evaluate_trigger,
)
from agentstack.storage.database import Database

ROWS = 400
WATERMARK = "fixture:abc123"
RULE = TargetingRule(
    profile="test",
    risk_quantile=0.9,
    minimum_cohort=10,
    minimum_annual_value_at_risk_cents=1_000,
)


@pytest.fixture(autouse=True)
def _registered(monkeypatch: pytest.MonkeyPatch) -> None:
    REGISTRY["fixture"] = DatasetSpec(
        key="fixture",
        kaggle="nobody/nothing",
        files=("fixture.csv",),
        target="Churn",
        churned="1",
        drops={},
        revenue_columns=("monthly charge",),
        revenue_periods_per_year=12,
        revenue_note="fixture revenue, annualised x12",
    )

    def only_the_fixture(key: str, root: Path | None = None) -> CohortSnapshot:
        # Answers for the fixture and nothing else: a loader returning the same rows
        # whatever it was asked would make every test here true of no dataset at all.
        assert key == "fixture", f"asked for {key!r}"
        assert root is None
        return snapshot()

    monkeypatch.setattr("agentstack.context.datasets.load", only_the_fixture)
    yield
    REGISTRY.pop("fixture", None)


def snapshot(watermark: str = WATERMARK) -> CohortSnapshot:
    frame = pd.DataFrame(
        {"monthly charge": [50.0] * ROWS, "Churn": [i % 4 == 0 for i in range(ROWS)]}
    ).astype({"Churn": int})
    return CohortSnapshot(
        dataset="fixture",
        data_as_of=watermark,
        rows=ROWS,
        columns=tuple(frame.columns),
        churn_rate=float(frame["Churn"].mean()),
        frame=frame,
    )


class StubScorer:
    """Ranks by row index. Standing in for TabPFN, which needs a licence to run."""

    model_version = "stub-1"

    def __init__(self) -> None:
        self.calls = 0

    def score(
        self,
        *,
        features: pd.DataFrame,
        labels: pd.Series,
        dataset: str,
        data_as_of: str,
    ) -> ChurnScores:
        self.calls += 1
        return ChurnScores(
            dataset=dataset,
            data_as_of=data_as_of,
            model_version=self.model_version,
            folds=5,
            seed=7,
            probabilities=tuple(i / len(features) for i in range(len(features))),
            positives=int(labels.sum()),
        )


def trigger(**overrides: object) -> TriggerEvent:
    payload = {
        "kind": "data_arrival",
        "experiment_id": "exp-7",
        "data_as_of": WATERMARK,
        "tenant": "acme",
    }
    payload.update(overrides)
    return parse_trigger(payload, source="test")


# --- the watermark names its own dataset ---


def test_the_dataset_is_read_from_the_watermark() -> None:
    assert dataset_of("telecom-bigml:f107d488") == "telecom-bigml"


def test_a_watermark_this_system_did_not_produce_is_refused() -> None:
    """A watermark that cannot name its dataset could not identify a population, which
    is the one thing it is for."""
    with pytest.raises(TriggerNotUnderstood, match="not a watermark"):
        dataset_of("whenever")


# --- the happy path ---


def test_a_trigger_becomes_a_frozen_cohort() -> None:
    scorer = StubScorer()

    evaluation = evaluate_trigger(trigger(), scorer=scorer, rule=RULE)

    assert evaluation.outcome is Outcome.PROPOSE
    assert evaluation.cohort is not None
    assert evaluation.cohort.size == 40
    assert evaluation.cohort.data_as_of == WATERMARK
    assert evaluation.cohort.model_version == "stub-1"
    assert scorer.calls == 1


def test_the_reason_is_readable_by_a_person() -> None:
    """It ends up in a Slack prompt. "PROPOSE" is not a quantity a human can weigh."""
    evaluation = evaluate_trigger(trigger(), scorer=StubScorer(), rule=RULE)

    assert "40 customers" in evaluation.reason
    assert "at risk" in evaluation.reason


def test_the_reason_states_the_cohorts_currency() -> None:
    """The reason is recorded in workflow history, so the USD string is pinned exactly:
    a change to it would make a recorded history disagree with this code."""
    usd = evaluate_trigger(trigger(), scorer=StubScorer(), rule=RULE)
    REGISTRY["fixture"] = replace(REGISTRY["fixture"], currency="NTD")
    ntd = evaluate_trigger(trigger(), scorer=StubScorer(), rule=RULE)

    assert usd.reason.endswith("$24,000 at risk")
    assert ntd.reason.endswith("NTD 24,000 at risk")
    assert "$" not in ntd.reason


def test_the_same_trigger_produces_the_same_experiment_version() -> None:
    first = evaluate_trigger(trigger(), scorer=StubScorer(), rule=RULE)
    second = evaluate_trigger(trigger(), scorer=StubScorer(), rule=RULE)

    assert first.cohort is not None and second.cohort is not None
    assert first.cohort.experiment_version == second.cohort.experiment_version


# --- refusals are answers, not errors ---


def test_a_cohort_that_misses_a_gate_abstains_rather_than_raising() -> None:
    """A cohort too small is a conclusion the cycle records and moves on from."""
    evaluation = evaluate_trigger(
        trigger(), scorer=StubScorer(), rule=replace(RULE, minimum_cohort=10_000)
    )

    assert evaluation.outcome is Outcome.ABSTAIN
    assert evaluation.cohort is None
    assert "Widening the cut" in evaluation.reason


def test_a_trigger_for_a_watermark_that_moved_abstains() -> None:
    """The trigger fired on one batch and the snapshot on disk is another. Scoring it
    anyway would produce a cohort that looks frozen and is not."""
    evaluation = evaluate_trigger(
        trigger(data_as_of="fixture:someone-elses-batch"), scorer=StubScorer(), rule=RULE
    )

    assert evaluation.outcome is Outcome.ABSTAIN
    assert "nobody asked about" in evaluation.reason


def test_an_unresolvable_watermark_is_a_failure_not_an_abstain() -> None:
    """With no population there is nothing to have concluded anything about."""
    with pytest.raises(TriggerNotUnderstood):
        evaluate_trigger(trigger(data_as_of="nonsense"), scorer=StubScorer(), rule=RULE)


# --- joined to the cycle ---


def test_the_seam_is_filled_and_the_cycle_records_the_version(
    app_database: Database,
) -> None:
    """Checkpoint C's sentence, as a test."""
    store = CycleStore(db=app_database)
    event = trigger()

    cycle = evaluate(
        store,
        event,
        lambda t: evaluate_trigger(t, scorer=StubScorer(), rule=RULE).as_seam_result(),
    )

    assert cycle.outcome is Outcome.PROPOSE
    assert cycle.run_id is not None and cycle.run_id.startswith("exp:")


def test_a_metric_movement_that_would_propose_is_still_refused(
    app_database: Database,
) -> None:
    """The wiring does not get to bypass the authority asymmetry. `evaluate_trigger`
    knows nothing about trigger kinds, and that is the point - the rule is enforced
    where the outcome is recorded."""
    from agentstack.policy.triggers import OutcomeNotAuthorized

    store = CycleStore(db=app_database)
    event = trigger(kind="metric_movement")

    with pytest.raises(OutcomeNotAuthorized):
        evaluate(
            store,
            event,
            lambda t: evaluate_trigger(t, scorer=StubScorer(), rule=RULE).as_seam_result(),
        )


def test_a_redelivered_trigger_does_not_score_the_cohort_again(
    app_database: Database,
) -> None:
    """Scoring is the expensive step. A broker redelivering must not pay for it twice."""
    store = CycleStore(db=app_database)
    event = trigger()
    scorer = StubScorer()

    for _ in range(3):
        evaluate(
            store,
            event,
            lambda t: evaluate_trigger(t, scorer=scorer, rule=RULE).as_seam_result(),
        )

    assert scorer.calls == 1
