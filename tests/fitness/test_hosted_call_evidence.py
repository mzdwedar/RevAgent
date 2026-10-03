"""Observability, evaluation, feedback: a call that sends cohort rows to a third party leaves
evidence, and the evidence holds no row and no secret.

Scoring is a read (ADR-0012), so the record is a trace span on an existing type, not an
audit record: `execution.read`, the same one the gateway emits for a read. What the span may
say is bounded by what the wrapper can see - sizes, a version, a duration, an outcome - and
it is never handed a feature value or the token.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from agentstack.execution.hosted_scorer import HostedTabPFNScorer
from agentstack.observability.spans import REQUIRED_SPANS, Span, Tracer, VersionStamp
from agentstack.prediction.churn import ScoringError
from agentstack.runtime.operator import TracedScorer

from .test_prediction_gate import GOOD_TOKEN, FakeHostedClient

SECRET_FEATURE = 987654.321


@pytest.fixture
def cohort() -> tuple[pd.DataFrame, pd.Series]:
    rows = 60
    features = pd.DataFrame({"salary": [SECRET_FEATURE + i for i in range(rows)]})
    labels = pd.Series([1 if i % 5 == 0 else 0 for i in range(rows)])
    return features, labels


def tracer() -> Tracer:
    stamp = VersionStamp(prompt="p", model="m", tool_schema="t", policy="y", retrieval="r")
    return Tracer(run_id="run-1", session_id="s-1", versions=stamp)


def hosted_scorer() -> HostedTabPFNScorer:
    FakeHostedClient.reset()
    return HostedTabPFNScorer(
        folds=4,
        build_classifier=FakeHostedClient,
        endpoint=lambda: "https://api.priorlabs.ai:443",
    )


def reads(t: Tracer) -> list[Span]:
    return [span for span in t.spans if span.name == "execution.read"]


def test_a_scoring_call_leaves_one_span_naming_its_size_version_latency_and_outcome(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    t = tracer()
    features, labels = cohort

    TracedScorer(hosted_scorer(), t).score(
        features=features, labels=labels, dataset="bank-churn", data_as_of="bank-churn:1"
    )

    (span,) = reads(t)
    assert span.attributes["tool"] == "churn.score"
    assert span.attributes["dataset"] == "bank-churn"
    assert span.attributes["rows"] == 60
    assert span.attributes["folds"] == 4
    assert span.attributes["model_version"] == "priorlabs:v3.5+9.1", "the version served"
    assert span.attributes["outcome"] == "scored"
    assert span.attributes["latency_ms"] >= 0


def test_a_refused_call_is_traced_as_failed_and_still_raises(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    t = tracer()
    scorer = hosted_scorer()
    FakeHostedClient.fail_on_fit = RuntimeError(f"quota exceeded for {SECRET_FEATURE}")
    features, labels = cohort

    with pytest.raises(ScoringError):
        TracedScorer(scorer, t).score(
            features=features, labels=labels, dataset="bank-churn", data_as_of="bank-churn:1"
        )

    (span,) = reads(t)
    assert span.attributes["outcome"] == "failed"
    assert span.attributes["error"] == "ScoringError"
    assert span.attributes["rows"] == 60
    assert "model_version" not in span.attributes, "nothing was served, so nothing is claimed"


def test_the_record_carries_no_feature_value_and_no_token(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    ok, failed = tracer(), tracer()
    features, labels = cohort

    TracedScorer(hosted_scorer(), ok).score(
        features=features, labels=labels, dataset="bank-churn", data_as_of="bank-churn:1"
    )
    scorer = hosted_scorer()
    FakeHostedClient.fail_on_fit = RuntimeError(f"rejected {GOOD_TOKEN} for {SECRET_FEATURE}")
    with pytest.raises(ScoringError):
        TracedScorer(scorer, failed).score(
            features=features, labels=labels, dataset="bank-churn", data_as_of="bank-churn:1"
        )

    for t in (ok, failed):
        written = json.dumps([span.attributes for span in t.spans], default=str)
        assert GOOD_TOKEN not in written
        assert "987654" not in written, "a feature value reached the trace"
        assert "salary" not in written, "a column name is cohort content too"


def test_the_wrapper_is_a_churn_scorer_and_answers_with_the_inner_scores(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    inner = hosted_scorer()
    traced = TracedScorer(inner, tracer())
    features, labels = cohort

    assert traced.model_version == inner.model_version
    assert traced.score(
        features=features, labels=labels, dataset="bank-churn", data_as_of="bank-churn:1"
    ) == inner.score(
        features=features, labels=labels, dataset="bank-churn", data_as_of="bank-churn:1"
    )


def test_the_evidence_uses_an_existing_span_type() -> None:
    """K10 adds no span type, so the required-span count does not move."""
    assert len(REQUIRED_SPANS) == 9
