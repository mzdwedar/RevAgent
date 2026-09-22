"""The real model, on the real cohorts. Needs TABPFN_TOKEN and `--extra prediction`.

CI proves our code handles the model's output correctly. It cannot prove the model
still works, and nothing recorded ever will. That is the honest limit of the fixture,
and this file is the part that closes it.

    export TABPFN_TOKEN=...
    uv sync --extra prediction
    uv run pytest tests/live -v
"""

from __future__ import annotations

import pytest

from agentstack.context import datasets
from agentstack.prediction.churn import RecordedScorer, ScoringError
from agentstack.prediction.engine import TabPFNScorer

KEYS = sorted(datasets.REGISTRY)


@pytest.fixture(scope="module")
def scorer() -> TabPFNScorer:
    engine = TabPFNScorer()
    engine.preflight()
    return engine


def test_preflight_loads_the_weights(scorer: TabPFNScorer) -> None:
    assert scorer.preflight() == "tabpfn-3.5"


@pytest.mark.parametrize("key", KEYS)
def test_the_recorded_scores_are_still_what_the_model_produces(
    key: str, scorer: TabPFNScorer
) -> None:
    """The fixture CI trusts, checked against the thing it was recorded from."""
    snapshot = datasets.load(key)
    target = datasets.REGISTRY[key].target
    try:
        recorded = RecordedScorer.load(key)
    except ScoringError as exc:
        pytest.fail(str(exc))

    fresh = scorer.score(
        features=snapshot.frame.drop(columns=[target]),
        labels=snapshot.frame[target],
        dataset=key,
        data_as_of=snapshot.data_as_of,
    )

    assert fresh.data_as_of == recorded.scores.data_as_of
    worst = max(
        abs(a - b) for a, b in zip(fresh.probabilities, recorded.scores.probabilities, strict=True)
    )
    assert worst < 0.01, (
        f"{key}: recorded and fresh scores differ by up to {worst:.4f}. Either the model "
        "changed or the cohort did; re-record only after working out which."
    )


@pytest.mark.parametrize("key", KEYS)
def test_the_scores_separate_churners_from_the_rest(key: str, scorer: TabPFNScorer) -> None:
    """A scorer that ranks no better than a coin makes targeting worse than random,
    and the experiment would then measure the cost of the offers and nothing else."""
    snapshot = datasets.load(key)
    target = datasets.REGISTRY[key].target
    scored = scorer.score(
        features=snapshot.frame.drop(columns=[target]),
        labels=snapshot.frame[target],
        dataset=key,
        data_as_of=snapshot.data_as_of,
    )

    frame = snapshot.frame.assign(p=scored.as_series(snapshot.frame.index))
    churners = frame.loc[frame[target] == 1, "p"].mean()
    rest = frame.loc[frame[target] == 0, "p"].mean()

    assert churners > rest + 0.1, f"{key}: churners {churners:.3f} vs others {rest:.3f}"


@pytest.mark.parametrize("key", KEYS)
def test_scoring_is_reproducible_across_processes(key: str, scorer: TabPFNScorer) -> None:
    """Criterion 17 for the model half: same snapshot, same seed, same numbers."""
    snapshot = datasets.load(key)
    target = datasets.REGISTRY[key].target
    features = snapshot.frame.drop(columns=[target])
    labels = snapshot.frame[target]

    first = scorer.score(
        features=features, labels=labels, dataset=key, data_as_of=snapshot.data_as_of
    )
    second = TabPFNScorer().score(
        features=features, labels=labels, dataset=key, data_as_of=snapshot.data_as_of
    )

    assert first.probabilities == second.probabilities
