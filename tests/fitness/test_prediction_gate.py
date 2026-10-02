"""Criterion 18: the licence gate fails at startup, not mid-run.

A run that parks on a human approval and only then discovers it cannot score has spent
someone's attention on work it could never finish.

These need neither a token nor the weights, which is the point: the gate's job is to
refuse when they are absent, and that is exactly the condition CI is in.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from agentstack.execution.hosted_scorer import (
    PRIORLABS_HOST,
    HostedClassifier,
    HostedTabPFNScorer,
    PriorLabsClassifier,
)
from agentstack.interfaces import preflight_cli
from agentstack.prediction import licence
from agentstack.prediction.churn import (
    ChurnScorer,
    ChurnScores,
    RecordedScorer,
    RecordedScorers,
    ScoringError,
    fold_assignment,
)
from agentstack.prediction.engine import MODEL_VERSION, TabPFNScorer, encode
from agentstack.prediction.licence import LicenceRefused

GOOD_TOKEN = "test-prediction-token"
ROOT = Path(__file__).resolve().parents[2]


def test_a_missing_token_is_refused_and_says_how_to_get_one() -> None:
    with pytest.raises(LicenceRefused) as caught:
        licence.check_token({})

    assert "TABPFN_TOKEN" in str(caught.value)
    assert "priorlabs.ai" in str(caught.value)


def test_a_blank_token_counts_as_missing() -> None:
    with pytest.raises(LicenceRefused, match="not set"):
        licence.check_token({"TABPFN_TOKEN": "   "})


def test_a_truncated_token_is_refused_as_a_typo_not_as_an_answer() -> None:
    """The message must not claim more than a length check can know."""
    with pytest.raises(LicenceRefused) as caught:
        licence.check_token({"TABPFN_TOKEN": "abc"})

    message = str(caught.value)
    assert "too short" in message
    assert "only loading the model proves a token is accepted" in message


def test_a_plausible_token_passes_the_cheap_check() -> None:
    assert licence.check_token({"TABPFN_TOKEN": GOOD_TOKEN}) == GOOD_TOKEN


def test_scoring_refuses_before_it_touches_the_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is the first thing `score` does. A scorer that read the cohort, encoded
    it, and *then* refused would have done the expensive part for nothing."""
    monkeypatch.delenv("TABPFN_TOKEN", raising=False)

    with pytest.raises(LicenceRefused):
        TabPFNScorer().score(
            features=pd.DataFrame({"x": [1.0, 2.0]}),
            labels=pd.Series([0, 1]),
            dataset="anything",
            data_as_of="anything",
        )


def test_preflight_refuses_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABPFN_TOKEN", raising=False)

    with pytest.raises(LicenceRefused, match="not set"):
        TabPFNScorer().preflight()


def test_the_startup_command_exits_nonzero_without_a_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What a deploy runs before shifting traffic."""
    monkeypatch.delenv("TABPFN_TOKEN", raising=False)

    assert preflight_cli.main([]) == 1
    assert "TABPFN_TOKEN" in capsys.readouterr().err


def test_the_token_only_check_says_what_it_did_not_verify(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A green check that quietly proves less than it appears to is worse than no check."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)

    assert preflight_cli.main(["--token-only"]) == 0
    out = capsys.readouterr().out
    assert "not checked" in out
    assert "whether the licence is accepted" in out


# --- reproducibility: the parts that must not depend on TabPFN at all ---


def test_folds_are_the_same_every_time() -> None:
    labels = [i % 3 == 0 for i in range(200)]

    assert fold_assignment(labels) == fold_assignment(labels)


def test_folds_are_stratified() -> None:
    """Churn is a 14-20% minority here. An unstratified split can hand a fold too few
    positives to condition on, and that fold's scores would be noise."""
    labels = [1 if i % 7 == 0 else 0 for i in range(210)]
    assignment = fold_assignment(labels, folds=5)

    per_fold = [
        sum(1 for i, fold_of in enumerate(assignment) if fold_of == fold and labels[i])
        for fold in range(5)
    ]

    assert min(per_fold) > 0
    assert max(per_fold) - min(per_fold) <= 1


def test_every_row_is_assigned_exactly_one_fold() -> None:
    assignment = fold_assignment([i % 2 for i in range(100)], folds=4)

    assert len(assignment) == 100
    assert set(assignment) == {0, 1, 2, 3}


def test_a_different_seed_assigns_differently() -> None:
    labels = [i % 2 for i in range(100)]

    assert fold_assignment(labels, seed=1) != fold_assignment(labels, seed=2)


def test_too_few_folds_to_score_out_of_sample_is_refused() -> None:
    with pytest.raises(ScoringError, match="cannot score anything out of sample"):
        fold_assignment([0, 1, 0, 1], folds=1)


def test_more_folds_than_rows_is_refused() -> None:
    with pytest.raises(ScoringError, match="cannot be split"):
        fold_assignment([0, 1], folds=5)


def test_category_encoding_does_not_depend_on_row_order() -> None:
    """Encoding by order of appearance would make a re-sorted snapshot produce
    different model inputs, and `data_as_of` would stop implying the same scores."""
    frame = pd.DataFrame({"plan": ["b", "a", "c", "a"], "n": [1.0, 2.0, 3.0, 4.0]})

    forward = encode(frame)
    backward = encode(frame.iloc[::-1]).iloc[::-1]

    assert list(forward["plan"]) == list(backward["plan"])


def test_mismatched_features_and_labels_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)

    with pytest.raises(ScoringError, match="3 rows of features, 2 labels"):
        TabPFNScorer().score(
            features=pd.DataFrame({"x": [1.0, 2.0, 3.0]}),
            labels=pd.Series([0, 1]),
            dataset="d",
            data_as_of="d:1",
        )


# --- recorded scores: how CI exercises the adapter without the weights ---


BASELINE = ChurnScores(
    dataset="fixture",
    data_as_of="fixture:abc123",
    model_version=MODEL_VERSION,
    folds=5,
    seed=7,
    probabilities=(0.1, 0.9, 0.3),
    positives=1,
)


def scores(**overrides: object) -> ChurnScores:
    return replace(BASELINE, **overrides)


def test_a_probability_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ScoringError, match="outside"):
        scores(probabilities=(0.5, 1.4))


def test_recorded_scores_refuse_a_different_snapshot() -> None:
    """Scores from one population say nothing about another. A replay that answered
    anyway would make the reproducibility tests pass while proving nothing."""
    scorer = RecordedScorer(scores=scores())

    with pytest.raises(ScoringError, match="Re-record"):
        scorer.score(
            features=pd.DataFrame({"x": [1.0, 2.0, 3.0]}),
            labels=pd.Series([0, 1, 0]),
            dataset="fixture",
            data_as_of="fixture:moved",
        )


def test_recorded_scores_refuse_a_different_number_of_rows() -> None:
    scorer = RecordedScorer(scores=scores())

    with pytest.raises(ScoringError, match="3 scores for 2 rows"):
        scorer.score(
            features=pd.DataFrame({"x": [1.0, 2.0]}),
            labels=pd.Series([0, 1]),
            dataset="fixture",
            data_as_of="fixture:abc123",
        )


def test_recorded_scores_refuse_the_same_rows_with_different_labels() -> None:
    """Same size, same snapshot id, different population. The row count alone would
    have let this through."""
    scorer = RecordedScorer(scores=scores())

    with pytest.raises(ScoringError, match="different population"):
        scorer.score(
            features=pd.DataFrame({"x": [1.0, 2.0, 3.0]}),
            labels=pd.Series([1, 1, 1]),
            dataset="fixture",
            data_as_of="fixture:abc123",
        )


def test_recorded_scores_replay_when_everything_matches() -> None:
    scorer = RecordedScorer(scores=scores())

    replayed = scorer.score(
        features=pd.DataFrame({"x": [1.0, 2.0, 3.0]}),
        labels=pd.Series([0, 1, 0]),
        dataset="fixture",
        data_as_of="fixture:abc123",
    )

    assert replayed.probabilities == (0.1, 0.9, 0.3)
    assert scorer.model_version == f"recorded:{MODEL_VERSION}"


def test_a_replay_is_labelled_as_one() -> None:
    """An experiment whose provenance says `tabpfn-3.5` when the numbers came from a
    file is a lie in the audit trail."""
    assert RecordedScorer(scores=scores()).model_version.startswith("recorded:")


def test_missing_recorded_scores_name_the_command_that_makes_them(tmp_path: Path) -> None:
    with pytest.raises(ScoringError, match="record_scores"):
        RecordedScorer.load("telecom-bigml", root=tmp_path)


def test_recorded_scores_round_trip_through_the_file_format(tmp_path: Path) -> None:
    recorded = scores()
    (tmp_path / "fixture.json").write_text(
        json.dumps(
            {
                "dataset": recorded.dataset,
                "data_as_of": recorded.data_as_of,
                "model_version": recorded.model_version,
                "folds": recorded.folds,
                "seed": recorded.seed,
                "probabilities": list(recorded.probabilities),
                "positives": recorded.positives,
            }
        )
    )

    assert RecordedScorer.load("fixture", root=tmp_path).scores == recorded


def test_the_provenance_names_the_snapshot_and_the_model() -> None:
    """What gets frozen into `experiment_version`: without both, a result cannot be
    reproduced and so cannot be rolled out to (criterion 17)."""
    provenance = scores().provenance()

    assert provenance["data_as_of"] == "fixture:abc123"
    assert provenance["model_version"] == MODEL_VERSION
    assert provenance["seed"] == 7
    assert provenance["scored_rows"] == 3


# --- cross-fitting: the property that stops the cohort measuring its own noise ---


class RecordingClassifier:
    """Returns the row's own index as a probability, and remembers what it was fit on.

    Not a stand-in for TabPFN. The thing under test is our fold loop: that every row is
    predicted by a model fit on the *other* folds. A real model would demonstrate that
    no better, far more slowly, and only on a machine with a licence.
    """

    fitted: list[set[int]] = []

    requested: list[str] = []

    def __init__(self, checkpoint: str) -> None:
        RecordingClassifier.requested.append(checkpoint)
        self.seen: set[int] = set()
        self.labelled: dict[int, int] = {}

    def fit(self, features: pd.DataFrame, labels: pd.Series) -> None:
        self.seen = {int(i) for i in features.index}
        self.labelled = {int(i): int(v) for i, v in zip(features.index, labels, strict=True)}
        RecordingClassifier.fitted.append(self.seen)

    def predict_proba(self, features: pd.DataFrame) -> list[tuple[float, float]]:
        asked = {int(i) for i in features.index}
        overlap = self.seen & asked
        assert not overlap, f"predicted rows the model was fit on: {sorted(overlap)[:5]}"
        leaked = self.labelled.keys() & asked
        assert not leaked, f"predicted rows whose label it was given: {sorted(leaked)[:5]}"
        # Never 0.0: an unscored row keeps its initialised 0.0, and a fake that also
        # returns 0.0 would make that bug invisible.
        return [((1000 - i) / 1000, (i + 1) / 1000) for i in range(len(features))]


@pytest.fixture
def cohort() -> tuple[pd.DataFrame, pd.Series]:
    rows = 60
    features = pd.DataFrame({"x": [float(i) for i in range(rows)]})
    labels = pd.Series([1 if i % 5 == 0 else 0 for i in range(rows)])
    return features, labels


def test_no_row_is_scored_by_a_model_that_saw_its_label(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    """In-sample scores would rank the rows the model fit best, not the customers most
    at risk — and the experiment would then measure regression to the mean."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    RecordingClassifier.fitted = []
    features, labels = cohort

    scored = TabPFNScorer(folds=4, build_classifier=RecordingClassifier).score(
        features=features, labels=labels, dataset="d", data_as_of="d:1"
    )

    assert len(scored.probabilities) == len(features)
    assert len(RecordingClassifier.fitted) == 4, "one model per fold"
    for seen in RecordingClassifier.fitted:
        assert len(seen) == len(features) * 3 // 4


def test_every_row_gets_a_score(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    """A row left at its initial 0.0 would look like the safest customer in the cohort."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    features, labels = cohort

    scored = TabPFNScorer(folds=4, build_classifier=RecordingClassifier).score(
        features=features, labels=labels, dataset="d", data_as_of="d:1"
    )

    assert all(p > 0.0 for p in scored.probabilities)


def test_the_scores_carry_the_snapshot_and_the_model_that_made_them(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    features, labels = cohort

    scored = TabPFNScorer(folds=4, build_classifier=RecordingClassifier).score(
        features=features, labels=labels, dataset="telecom-bigml", data_as_of="telecom-bigml:abc"
    )

    assert scored.dataset == "telecom-bigml"
    assert scored.data_as_of == "telecom-bigml:abc"
    assert scored.model_version == MODEL_VERSION
    assert scored.positives == int(labels.sum())


def test_scoring_the_same_cohort_twice_gives_the_same_numbers(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    features, labels = cohort
    scorer = TabPFNScorer(folds=4, build_classifier=RecordingClassifier)

    first = scorer.score(features=features, labels=labels, dataset="d", data_as_of="d:1")
    second = scorer.score(features=features, labels=labels, dataset="d", data_as_of="d:1")

    assert first.probabilities == second.probabilities


def test_without_the_optional_extra_the_message_names_it(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    """The extra is absent by design, and this test does not rely on that.

    Asserting an ImportError by simply not installing the package makes the test pass
    for a reason outside the repository: anyone who runs `uv sync --extra prediction`
    flips it to red, having changed nothing. Hiding the module makes the condition the
    test's own.
    """
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    monkeypatch.setitem(sys.modules, "tabpfn", None)
    features, labels = cohort

    with pytest.raises(ScoringError, match="--extra prediction"):
        TabPFNScorer(folds=4).score(features=features, labels=labels, dataset="d", data_as_of="d:1")


def test_preflight_reports_a_missing_extra_rather_than_blaming_the_licence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Your licence is bad" when the real problem is an uninstalled package sends
    someone to the wrong website."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    monkeypatch.setitem(sys.modules, "tabpfn", None)

    with pytest.raises(ScoringError, match="--extra prediction"):
        TabPFNScorer().preflight()

    assert not isinstance(ScoringError("x"), LicenceRefused)


class WorkingClassifier:
    """Fits and predicts, with no opinion about overlap.

    `RecordingClassifier` refuses to predict a row it was fit on, which is right for
    scoring and wrong here: preflight deliberately fits and predicts the same four
    rows, because it is asking "can the weights load at all", not "what is this
    customer's risk".
    """

    def __init__(self, checkpoint: str) -> None:
        self.checkpoint = checkpoint

    def fit(self, features: pd.DataFrame, labels: pd.Series) -> None:
        assert len(features) == len(labels)

    def predict_proba(self, features: pd.DataFrame) -> list[tuple[float, float]]:
        return [(0.5, 0.5)] * len(features)


def test_preflight_passes_when_the_model_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)

    assert TabPFNScorer(build_classifier=WorkingClassifier).preflight() == MODEL_VERSION


def test_a_refused_licence_at_load_time_is_reported_as_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw exception from inside a dependency, at startup, tells an operator nothing.

    This is the branch the cheap token check explicitly cannot reach: a well-formed key
    the issuer does not accept.
    """
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)

    class Rejected:
        def __init__(self, checkpoint: str) -> None:
            self.checkpoint = checkpoint

        def fit(self, features: pd.DataFrame, labels: pd.Series) -> None:
            raise RuntimeError(
                f"403 Forbidden: licence not accepted (asked about {len(features)} rows, "
                f"{int(labels.sum())} positive)"
            )

    with pytest.raises(LicenceRefused, match="could not load its weights"):
        TabPFNScorer(build_classifier=Rejected).preflight()


def test_an_empty_fold_is_skipped_rather_than_scored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Possible when a class is smaller than the fold count. Scoring an empty fold
    would fit a model on everything and predict nothing."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    RecordingClassifier.fitted = []
    features = pd.DataFrame({"x": [float(i) for i in range(6)]})
    labels = pd.Series([0, 0, 0, 0, 0, 1])

    scored = TabPFNScorer(folds=5, build_classifier=RecordingClassifier).score(
        features=features, labels=labels, dataset="d", data_as_of="d:1"
    )

    assert len(scored.probabilities) == 6
    assert len(RecordingClassifier.fitted) <= 5


def test_scores_become_a_series_aligned_to_the_cohort() -> None:
    """Targeting joins scores back onto the snapshot; a misaligned index would offer
    the discount to the wrong customers."""
    index = pd.Index([10, 11, 12], name="row")

    series = scores().as_series(index)

    assert list(series.index) == [10, 11, 12]
    assert series.name == "churn_probability"
    assert list(series) == [0.1, 0.9, 0.3]


def test_the_checkpoint_is_named_not_inherited() -> None:
    """`tabpfn` resolves a bare classifier against its own default version, which is
    the package's to change. `MODEL_VERSION` travels in `experiment_version`, so an
    upgrade must not be able to make a recorded provenance wrong."""
    from agentstack.prediction.engine import CHECKPOINT

    assert CHECKPOINT == "v3.5"
    assert MODEL_VERSION == "tabpfn-3.5"
    assert f"tabpfn-{CHECKPOINT.removeprefix('v')}" == MODEL_VERSION


def test_the_non_commercial_licence_is_recorded_where_it_is_relevant() -> None:
    """TabPFN-3.5 is open to read and run, not open to ship commercially. This system
    is built to production shape, so that constrains deploying it as one."""
    from agentstack.prediction import engine

    assert engine.__doc__ is not None
    source = Path(engine.__file__).read_text()
    assert "non-commercial" in source


def test_the_scorer_asks_for_the_checkpoint_it_claims_to_use(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    """`MODEL_VERSION` is recorded in every experiment's provenance. This is the test
    that it describes the checkpoint actually requested, rather than whichever one the
    package happened to default to."""
    from agentstack.prediction.engine import CHECKPOINT

    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    RecordingClassifier.requested = []
    features, labels = cohort

    TabPFNScorer(folds=4, build_classifier=RecordingClassifier).score(
        features=features, labels=labels, dataset="d", data_as_of="d:1"
    )

    assert set(RecordingClassifier.requested) == {CHECKPOINT}
    assert CHECKPOINT == "v3.5"


def test_the_readme_leads_with_the_non_commercial_constraint() -> None:
    """A licence that forbids the use this system is shaped for is not a footnote.

    Tested because it is prose, and prose is what gets trimmed when someone is tidying
    up. It has to survive, and it has to stay above the setup instructions - a reader
    who has already run `uv sync` has stopped reading.
    """
    readme = (ROOT / "README.md").read_text()

    assert "non-commercial" in readme
    assert readme.index("non-commercial") < readme.index("## Quickstart"), (
        "the licence constraint has drifted below the setup steps"
    )
    assert "ux.priorlabs.ai" in readme
    assert "cannot be shipped in a commercial product" in readme


def test_recorded_scorers_answer_for_the_dataset_asked_about_and_no_other() -> None:
    """A worker serves whichever cohort a trigger names. One recording per dataset, and
    a dataset nobody recorded is an error, not the nearest one."""
    scorers = RecordedScorers(by_dataset={"fixture": RecordedScorer(scores=scores())})
    asked = {
        "features": pd.DataFrame({"x": [1.0, 2.0, 3.0]}),
        "labels": pd.Series([0, 1, 0]),
        "data_as_of": "fixture:abc123",
    }

    assert scorers.score(dataset="fixture", **asked) == BASELINE
    assert scorers.model_version == f"recorded:{BASELINE.model_version}"
    with pytest.raises(ScoringError, match="no recorded scores for 'other'"):
        scorers.score(dataset="other", **asked)


def test_recorded_scorers_load_every_recording_and_refuse_an_empty_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(ScoringError, match="no recorded scores"):
        RecordedScorers.load(root=tmp_path)

    (tmp_path / "fixture.json").write_text(
        json.dumps(
            {
                "dataset": BASELINE.dataset,
                "data_as_of": BASELINE.data_as_of,
                "model_version": BASELINE.model_version,
                "folds": BASELINE.folds,
                "seed": BASELINE.seed,
                "probabilities": list(BASELINE.probabilities),
                "positives": BASELINE.positives,
            }
        )
    )
    loaded = RecordedScorers.load(root=tmp_path)
    assert set(loaded.by_dataset) == {"fixture"}


def test_the_worker_chooses_recorded_scores_only_when_asked_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentstack.interfaces import worker_cli

    monkeypatch.setattr(RecordedScorers, "load", staticmethod(lambda: RecordedScorers({})))
    assert isinstance(worker_cli._scorer("recorded"), RecordedScorers)
    assert isinstance(worker_cli._scorer("tabpfn"), TabPFNScorer)


# --- the hosted scorer (layer 7): the same out-of-fold property, over a network seam ---


class FakeHostedClient:
    """Mirrors the verified `tabpfn-client` surface (ADR-0012 4a): fit, predict_proba, and
    a served version read back after predict. Like `RecordingClassifier`, it is not a
    stand-in for the vendor; it proves our fold loop and our refusals."""

    fitted: list[set[int]] = []
    requested: list[str] = []
    served: str | None = "v3.5+9.1"
    fail_on_fit: Exception | None = None
    fit_calls = 0

    def __init__(self, checkpoint: str) -> None:
        FakeHostedClient.requested.append(checkpoint)
        self.seen: set[int] = set()

    @classmethod
    def reset(cls) -> None:
        cls.fitted = []
        cls.requested = []
        cls.served = "v3.5+9.1"
        cls.fail_on_fit = None
        cls.fit_calls = 0

    def fit(self, features: pd.DataFrame, labels: pd.Series) -> None:
        FakeHostedClient.fit_calls += 1
        if FakeHostedClient.fail_on_fit is not None:
            raise FakeHostedClient.fail_on_fit
        assert len(features) == len(labels)
        self.seen = {int(i) for i in features.index}
        FakeHostedClient.fitted.append(self.seen)

    def predict_proba(self, features: pd.DataFrame) -> list[tuple[float, float]]:
        asked = {int(i) for i in features.index}
        assert not (self.seen & asked), "predicted rows the model was fit on"
        return [((1000 - i) / 1000, (i + 1) / 1000) for i in range(len(features))]

    def served_model_version(self) -> str | None:
        return FakeHostedClient.served


def hosted(
    *,
    build_classifier: Callable[[str], HostedClassifier] = FakeHostedClient,
    endpoint: Callable[[], str] = lambda: "https://api.priorlabs.ai:443",
) -> HostedTabPFNScorer:
    FakeHostedClient.reset()
    return HostedTabPFNScorer(folds=4, build_classifier=build_classifier, endpoint=endpoint)


def score_hosted(scorer: HostedTabPFNScorer, cohort: tuple[pd.DataFrame, pd.Series]) -> ChurnScores:
    features, labels = cohort
    return scorer.score(features=features, labels=labels, dataset="d", data_as_of="d:1")


def test_the_hosted_scorer_is_a_churn_scorer() -> None:
    scorer: ChurnScorer = hosted()

    assert scorer.model_version.startswith("priorlabs:")


def test_no_row_is_scored_by_a_hosted_model_that_saw_its_label(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    """Fold assignment and encoding stay ours, so the property is checkable even though
    the model is not: each row's score comes from a fit on the other folds only."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)

    scored = score_hosted(hosted(), cohort)

    assert len(scored.probabilities) == len(cohort[0])
    assert len(FakeHostedClient.fitted) == 4, "one hosted fit per fold"
    assert all(len(seen) == len(cohort[0]) * 3 // 4 for seen in FakeHostedClient.fitted)
    assert all(p > 0.0 for p in scored.probabilities)


def test_the_hosted_scorer_uses_the_same_folds_as_the_local_one(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    """Same snapshot and seed, same cohort. Folds are ours in both scorers."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    RecordingClassifier.fitted = []
    features, labels = cohort

    TabPFNScorer(folds=4, build_classifier=RecordingClassifier).score(
        features=features, labels=labels, dataset="d", data_as_of="d:1"
    )
    local = list(RecordingClassifier.fitted)
    score_hosted(hosted(), cohort)

    assert FakeHostedClient.fitted == local


def test_the_served_version_is_recorded_not_assumed(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)

    scored = score_hosted(hosted(), cohort)

    assert scored.model_version == "priorlabs:v3.5+9.1"
    assert set(FakeHostedClient.requested) == {"v3.5"}


@pytest.mark.parametrize("served", [None, "", "  "])
def test_a_version_the_service_does_not_report_is_refused(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series], served: str | None
) -> None:
    """An experiment whose provenance names a model it cannot name is worse than none."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    scorer = hosted()
    FakeHostedClient.served = served

    with pytest.raises(ScoringError, match="did not report which model version"):
        score_hosted(scorer, cohort)


def test_a_version_that_moves_between_folds_is_refused(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    """One cohort, two models, no honest single label for it."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)

    class Moving(FakeHostedClient):
        def served_model_version(self) -> str | None:
            return f"v3.5+{len(FakeHostedClient.fitted)}"

    with pytest.raises(ScoringError, match="changed model version"):
        score_hosted(hosted(build_classifier=Moving), cohort)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example:443",
        "http://api.priorlabs.ai:443",
        "https://api.priorlabs.ai.evil.example:443",
        "https://api.priorlabs.ai:8443",
        "http://localhost:8080",
    ],
)
def test_a_host_other_than_priorlabs_is_refused_before_any_row_leaves(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series], endpoint: str
) -> None:
    """`TABPFN_CLIENT_API_URL` can redirect the client; the cohort must not follow it."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    scorer = hosted(endpoint=lambda: endpoint)

    with pytest.raises(ScoringError, match="api.priorlabs.ai"):
        score_hosted(scorer, cohort)

    assert FakeHostedClient.fit_calls == 0


def test_a_transport_failure_is_a_scoring_error_not_a_retry_or_a_guess(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    scorer = hosted()
    FakeHostedClient.fail_on_fit = ConnectionError("quota exceeded")

    with pytest.raises(ScoringError, match="quota exceeded"):
        score_hosted(scorer, cohort)

    assert FakeHostedClient.fit_calls == 1, "no silent retry, no next fold"


def test_the_hosted_scorer_refuses_without_a_token_before_it_calls_out(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.delenv("TABPFN_TOKEN", raising=False)
    scorer = hosted()

    with pytest.raises(LicenceRefused):
        score_hosted(scorer, cohort)

    assert FakeHostedClient.fit_calls == 0


def test_the_hosted_scorer_refuses_mismatched_features_and_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)

    with pytest.raises(ScoringError, match="3 rows of features, 2 labels"):
        hosted().score(
            features=pd.DataFrame({"x": [1.0, 2.0, 3.0]}),
            labels=pd.Series([0, 1]),
            dataset="d",
            data_as_of="d:1",
        )


def test_without_the_client_extra_the_message_names_it(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    monkeypatch.setitem(sys.modules, "tabpfn_client", None)
    monkeypatch.setitem(sys.modules, "tabpfn_client.client", None)

    with pytest.raises(ScoringError, match="--extra prediction"):
        score_hosted(HostedTabPFNScorer(folds=4), cohort)


def test_the_adapter_reads_the_version_from_the_predict_metadata() -> None:
    """The one place we touch the vendor's metadata: `billing_model_version` is the
    server's own statement, `package_version` the build that ran it."""

    class Underlying:
        _last_meta: dict[str, object] = {}

        def fit(self, features: pd.DataFrame, labels: pd.Series) -> object:
            assert len(features) == len(labels)
            return self

        def predict_proba(self, features: pd.DataFrame) -> list[tuple[float, float]]:
            return [(0.4, 0.6)] * len(features)

    wrapped = PriorLabsClassifier(Underlying())
    assert wrapped.served_model_version() is None, "nothing predicted yet, nothing to report"

    Underlying._last_meta = {"package_version": "9.1"}
    assert wrapped.served_model_version() is None, "no billing_model_version: unrecordable"

    Underlying._last_meta = {"billing_model_version": "v3.5", "package_version": "9.1"}
    assert wrapped.served_model_version() == "v3.5+9.1"


def test_the_pinned_host_is_a_constant() -> None:
    assert PRIORLABS_HOST == "api.priorlabs.ai"


def test_a_scoring_error_from_the_client_passes_through_unwrapped(
    monkeypatch: pytest.MonkeyPatch, cohort: tuple[pd.DataFrame, pd.Series]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    scorer = hosted()
    FakeHostedClient.fail_on_fit = ScoringError("train set above the service limit")

    with pytest.raises(ScoringError) as caught:
        score_hosted(scorer, cohort)

    assert str(caught.value) == "train set above the service limit"


def test_the_real_client_asks_for_the_named_checkpoint_and_the_pinned_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two seams' defaults, exercised without a call: constructing the client and
    reading its base URL are local. Telemetry is switched off before the import."""
    monkeypatch.setenv("TABPFN_DISABLE_TELEMETRY", "1")
    monkeypatch.delenv("TABPFN_CLIENT_API_URL", raising=False)
    from agentstack.execution import hosted_scorer

    classifier = hosted_scorer._build_priorlabs_classifier("v3.5")

    assert isinstance(classifier, PriorLabsClassifier)
    assert classifier._underlying.model_path == "v3.5_default"
    hosted_scorer.check_host(hosted_scorer._client_endpoint())


def test_the_real_client_builder_names_the_extra_when_it_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentstack.execution import hosted_scorer

    monkeypatch.setitem(sys.modules, "tabpfn_client", None)

    with pytest.raises(ScoringError, match="--extra prediction"):
        hosted_scorer._build_priorlabs_classifier("v3.5")


# --- K9: the hosted scorer behind the same startup gate ---


def test_hosted_preflight_refuses_without_a_token_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TABPFN_TOKEN", raising=False)
    scorer = hosted()

    with pytest.raises(LicenceRefused, match="not set"):
        scorer.preflight()

    assert FakeHostedClient.fit_calls == 0


def test_hosted_preflight_refuses_a_short_token_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", "abc")
    scorer = hosted()

    with pytest.raises(LicenceRefused, match="too short"):
        scorer.preflight()

    assert FakeHostedClient.fit_calls == 0


def test_hosted_preflight_refuses_a_foreign_host_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    scorer = hosted(endpoint=lambda: "https://evil.example:443")

    with pytest.raises(ScoringError, match="api.priorlabs.ai"):
        scorer.preflight()

    assert FakeHostedClient.fit_calls == 0


def test_hosted_preflight_passes_and_names_the_version_the_service_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    scorer = hosted(build_classifier=WorkingHostedClient)
    WorkingHostedClient.served = "v3.5+9.1"

    assert scorer.preflight() == "priorlabs:v3.5+9.1"


def test_hosted_preflight_that_cannot_learn_the_version_fails_as_a_licence_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the service will not say what it ran, scoring would refuse every cohort later."""
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    scorer = hosted(build_classifier=WorkingHostedClient)
    WorkingHostedClient.served = None

    with pytest.raises(LicenceRefused, match="did not report"):
        scorer.preflight()


def test_a_rejected_key_at_the_hosted_preflight_is_reported_as_a_licence_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    scorer = hosted()
    FakeHostedClient.fail_on_fit = ConnectionError("401 invalid token")

    with pytest.raises(LicenceRefused, match="401 invalid token"):
        scorer.preflight()

    assert FakeHostedClient.fit_calls == 1, "one attempt, no retry"


class WorkingHostedClient:
    served: str | None = "v3.5+9.1"

    def __init__(self, checkpoint: str) -> None:
        self.checkpoint = checkpoint

    def fit(self, features: pd.DataFrame, labels: pd.Series) -> None:
        assert len(features) == len(labels)

    def predict_proba(self, features: pd.DataFrame) -> list[tuple[float, float]]:
        return [(0.5, 0.5)] * len(features)

    def served_model_version(self) -> str | None:
        return WorkingHostedClient.served


def test_the_preflight_command_runs_the_hosted_variant_when_asked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TABPFN_TOKEN", GOOD_TOKEN)
    monkeypatch.setattr(
        preflight_cli,
        "HostedTabPFNScorer",
        lambda: hosted(build_classifier=WorkingHostedClient),
    )
    WorkingHostedClient.served = "v3.5+9.1"

    assert preflight_cli.main(["--scorer", "hosted"]) == 0
    assert "priorlabs:v3.5+9.1" in capsys.readouterr().out


def test_the_hosted_preflight_command_checks_the_token_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TABPFN_TOKEN", raising=False)
    built: list[int] = []
    monkeypatch.setattr(preflight_cli, "HostedTabPFNScorer", lambda: built.append(1) or hosted())

    assert preflight_cli.main(["--scorer", "hosted"]) == 1
    assert "TABPFN_TOKEN" in capsys.readouterr().err
    assert FakeHostedClient.fit_calls == 0


def test_the_worker_can_score_through_the_hosted_service_and_preflights_that_variant() -> None:
    from agentstack.interfaces import worker_cli

    assert isinstance(worker_cli._scorer("hosted"), HostedTabPFNScorer)

    seen: list[list[str]] = []

    def refuse(argv: list[str]) -> int:
        seen.append(argv)
        return 1

    assert worker_cli.main(["--scores", "hosted"], preflight=refuse) == 1
    assert worker_cli.main(["--scores", "tabpfn"], preflight=refuse) == 1
    assert seen == [["--scorer", "hosted"], []]


def test_importing_the_hosted_client_switches_its_telemetry_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0012 4a: the client's usage events go to a second third party, PostHog."""
    from agentstack.execution import hosted_scorer

    monkeypatch.setenv("TABPFN_DISABLE_TELEMETRY", "0")
    monkeypatch.setitem(sys.modules, "tabpfn_client", None)

    with pytest.raises(ScoringError):
        hosted_scorer._build_priorlabs_classifier("v3.5")

    assert os.environ["TABPFN_DISABLE_TELEMETRY"] == "1"


def test_the_scripts_that_need_the_token_read_it_from_dot_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = importlib.util.spec_from_file_location(
        "record_scores", ROOT / "scripts/record_scores.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    env = tmp_path / ".env"
    env.write_text(
        "# a comment\nTABPFN_TOKEN='from-the-file'\nOTHER=\"x y\"\nBARE=1\n\nnot a pair\n"
    )
    monkeypatch.delenv("TABPFN_TOKEN", raising=False)
    monkeypatch.delenv("OTHER", raising=False)
    monkeypatch.setenv("BARE", "from-the-shell")

    module.load_env(env)

    assert os.environ["TABPFN_TOKEN"] == "from-the-file"
    assert os.environ["OTHER"] == "x y"
    assert os.environ["BARE"] == "from-the-shell", "the real environment wins"
    module.load_env(tmp_path / "missing.env")  # absent file is not an error
