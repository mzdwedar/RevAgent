"""Churn probabilities for a cohort, scored out of fold.

**Out of fold, not in sample.** TabPFN is zero-training, but it still conditions on the
rows it is given, so asking it about a customer it has already seen labelled returns a
number that is partly memory. Targeting the top decile of in-sample scores would select
the rows the model fit best rather than the customers most at risk - and the experiment
would then measure regression to the mean, which is the failure this design already
names as the easiest way to get a confident wrong answer.

So every row is scored by a model that did not see its label: the cohort is split into
folds, and each fold is predicted from the others.

**The fold assignment is ours, not the library's.** It is the part reproducibility
depends on - the same snapshot and the same seed must give the same cohort - so it is
written here, in twelve lines that CI can check, rather than delegated to a dependency
whose shuffling could change between releases.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd

DEFAULT_FOLDS = 5
DEFAULT_SEED = 20260922
SCORES_ROOT = Path(__file__).resolve().parents[3] / "data" / "scores"


class ScoringError(RuntimeError):
    """The scores asked for cannot be produced, or cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ChurnScores:
    """Probabilities, plus everything needed to say what produced them.

    `data_as_of` and `model_version` travel with the scores because a cohort frozen
    against one snapshot and scored by another model is not reproducible, and an
    experiment that cannot be reproduced cannot be rolled out to (criterion 17).
    """

    dataset: str
    data_as_of: str
    model_version: str
    folds: int
    seed: int
    probabilities: tuple[float, ...]
    # How many of the scored rows actually churned. Recorded so a replay can check it
    # is being asked about the same labels, not merely the same number of rows.
    positives: int

    def __post_init__(self) -> None:
        bad = [p for p in self.probabilities if not 0.0 <= p <= 1.0]
        if bad:
            raise ScoringError(f"{len(bad)} score(s) outside [0, 1]: {bad[:3]}")

    def as_series(self, index: pd.Index | None = None) -> pd.Series:
        return pd.Series(self.probabilities, index=index, name="churn_probability")

    def provenance(self) -> dict[str, object]:
        """What gets frozen into `experiment_version`."""
        return {
            "dataset": self.dataset,
            "data_as_of": self.data_as_of,
            "model_version": self.model_version,
            "folds": self.folds,
            "seed": self.seed,
            "scored_rows": len(self.probabilities),
            "positives": self.positives,
        }


class ChurnScorer(Protocol):
    """What the runtime may ask for. Note what is absent: any notion of a threshold."""

    @property
    def model_version(self) -> str: ...

    def score(
        self,
        *,
        features: pd.DataFrame,
        labels: pd.Series,
        dataset: str,
        data_as_of: str,
    ) -> ChurnScores: ...


def fold_assignment(
    labels: Sequence[int], *, folds: int = DEFAULT_FOLDS, seed: int = DEFAULT_SEED
) -> list[int]:
    """Deterministic stratified folds.

    Stratified because churn is a 14-20% minority here: an unstratified split can hand
    a fold too few positives to condition on. Deterministic because the same snapshot
    and seed must produce the same cohort, every time, in any process.
    """
    if folds < 2:
        raise ScoringError(f"{folds} folds cannot score anything out of sample")
    if len(labels) < folds:
        raise ScoringError(f"{len(labels)} rows cannot be split into {folds} folds")

    assignment = [0] * len(labels)
    for label in sorted(set(labels)):
        members = [i for i, value in enumerate(labels) if value == label]
        # Rotate by a label-dependent offset so the classes do not stack the same fold.
        offset = (seed + int(label) * 7919) % folds
        for position, index in enumerate(members):
            assignment[index] = (position + offset) % folds
    return assignment


@dataclass(frozen=True, slots=True)
class RecordedScorer:
    """Replays scores recorded from a real run.

    This is how CI exercises the adapter without the weights, the licence or a GPU. It
    is not a stub: the numbers came from TabPFN, and the file records which snapshot
    they came from. Asked about a different snapshot it refuses, because scores from
    one population say nothing about another - a fake that answered anyway would make
    the reproducibility tests pass while proving nothing.
    """

    scores: ChurnScores

    @property
    def model_version(self) -> str:
        return f"recorded:{self.scores.model_version}"

    @staticmethod
    def load(dataset: str, *, root: Path = SCORES_ROOT) -> RecordedScorer:
        path = root / f"{dataset}.json"
        if not path.exists():
            raise ScoringError(
                f"no recorded scores at {path}. They are produced from a real TabPFN run: "
                f"set TABPFN_TOKEN and run `uv run python scripts/record_scores.py`."
            )
        raw = json.loads(path.read_text())
        return RecordedScorer(
            scores=ChurnScores(
                dataset=raw["dataset"],
                data_as_of=raw["data_as_of"],
                model_version=raw["model_version"],
                folds=raw["folds"],
                seed=raw["seed"],
                probabilities=tuple(raw["probabilities"]),
                positives=raw["positives"],
            )
        )

    def score(
        self,
        *,
        features: pd.DataFrame,
        labels: pd.Series,
        dataset: str,
        data_as_of: str,
    ) -> ChurnScores:
        if dataset != self.scores.dataset or data_as_of != self.scores.data_as_of:
            raise ScoringError(
                f"recorded scores are for {self.scores.dataset} at {self.scores.data_as_of}; "
                f"asked about {dataset} at {data_as_of}. Re-record before trusting these."
            )
        if len(features) != len(self.scores.probabilities):
            raise ScoringError(
                f"recorded {len(self.scores.probabilities)} scores for {len(features)} rows"
            )
        positives = int(labels.sum())
        if positives != self.scores.positives:
            raise ScoringError(
                f"recorded scores were computed against {self.scores.positives} churners; "
                f"these labels have {positives}. Same row count, different population."
            )
        return self.scores
