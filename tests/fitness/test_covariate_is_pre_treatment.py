"""Learning loop L3 (SPEC criterion 4): the covariate is known before the offer.

It is built from the recorded cross-fitted scores and an observed payment, nothing else, so
these tests hold what makes it safe as an ANCOVA regressor: it refuses another snapshot, it
carries the pinned model version, and it does not depend on the outcome.
"""

from __future__ import annotations

import inspect

import pytest

from agentstack.prediction.churn import ChurnScores, ScoringError
from agentstack.prediction.covariate import DEFINITION, eltv_pre
from agentstack.prediction.engine import MODEL_VERSION


def _scores(as_of: str = "ds:1") -> ChurnScores:
    return ChurnScores("ds", as_of, MODEL_VERSION, 5, 1, (0.0, 0.25, 1.0), 1)


def test_the_value_is_survival_times_payment() -> None:
    got = eltv_pre(_scores(), [14900, 14900, 14900], data_as_of="ds:1")
    assert got.values == (14900.0, 11175.0, 0.0)


def test_it_carries_the_pinned_model_and_snapshot() -> None:
    got = eltv_pre(_scores(), [1, 1, 1], data_as_of="ds:1")
    assert got.model_version == MODEL_VERSION
    assert got.data_as_of == "ds:1"
    assert got.provenance()["definition"] == DEFINITION


def test_scores_from_another_snapshot_are_refused() -> None:
    with pytest.raises(ScoringError, match="not pre-treatment"):
        eltv_pre(_scores("ds:0"), [1, 1, 1], data_as_of="ds:1")


def test_a_length_mismatch_is_refused() -> None:
    with pytest.raises(ScoringError, match="payments"):
        eltv_pre(_scores(), [1, 1], data_as_of="ds:1")


def test_the_covariate_takes_no_outcome() -> None:
    assert set(inspect.signature(eltv_pre).parameters) == {
        "scores",
        "payment_cents",
        "data_as_of",
    }
