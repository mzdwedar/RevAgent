"""The real scorer: PriorLabs TabPFN-3.5, cross-fitted over the cohort.

Named `engine`, not `tabpfn`: a module that imports a top-level package of its own
name is a trap waiting for the first person who adds a relative import.

`tabpfn` is an optional dependency and is imported inside the methods that need it.
It pulls torch, which is large, needs a GPU to be worth anything, and is irrelevant to
every other part of this system - a CI run that lints the approval boundary should not
be installing a deep-learning stack to do it. `uv sync --extra prediction` installs it.

On the boundary question: this module downloads model weights over the network, and
`CONSTRAINTS.md` says only `agentstack.execution` reaches the world. Fetching a model
asset is not an execution surface. Model engine & inference separates the model *asset* from the
serving
system from the interaction contract; pulling the asset is closer to pulling a
container image than to issuing a rollout - it happens once at startup, it is not the
agent acting on anyone, and it is gated by `licence.preflight` rather than by policy
and approval. `lint-imports` contract 3 still forbids this package an HTTP client of
its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from agentstack.prediction.churn import (
    DEFAULT_FOLDS,
    DEFAULT_SEED,
    ChurnScores,
    ScoringError,
    fold_assignment,
)
from agentstack.prediction.licence import LicenceRefused, check_token

# The checkpoint, named explicitly rather than inherited.
#
# `tabpfn` picks a version from `settings.model_version` when the classifier is built
# bare, and that default is the package's to change. `MODEL_VERSION` goes into every
# experiment's provenance, so taking the default would mean a package upgrade could
# silently make a recorded version wrong - and an experiment whose provenance names a
# model it did not use is worse than one with no provenance at all.
CHECKPOINT = "v3.5"
MODEL_VERSION = f"tabpfn-{CHECKPOINT.removeprefix('v')}"

# TabPFN-3.5's weights are open under a **non-commercial** licence, accepted once per
# machine through the Hugging Face gate (see `licence.py`). Open to read and run; not
# open to ship in a commercial product. This system is built to production shape, so
# that is a real constraint on deploying it as one and not merely a setup step.


@dataclass(frozen=True, slots=True)
class TabPFNScorer:
    folds: int = DEFAULT_FOLDS
    seed: int = DEFAULT_SEED
    # The classifier is a seam so the cross-fitting loop can be tested without a GPU.
    # What that loop guarantees - no row is predicted by a model that saw its label -
    # is the property stopping the targeted cohort from measuring regression to the
    # mean, and it is ours, not TabPFN's. A fake that records which rows it was fit on
    # proves it exactly; a real model would prove it no better and far more slowly.
    #
    # It takes the checkpoint name so that choice is observable too: "this scorer asks
    # for v3.5" is the claim `MODEL_VERSION` makes in every experiment's provenance,
    # and a claim worth recording is worth being able to check.
    build_classifier: Callable[[str], Any] | None = None
    # bank-churn's training folds are 8,000 rows, above the 5,000-sample soft limit
    # that applies outside CUDA. The benchmark set this too; it is a limit on the
    # advertised operating range, not a correctness switch.
    ignore_pretraining_limits: bool = True

    @property
    def model_version(self) -> str:
        return MODEL_VERSION

    def _classifier(self) -> Any:
        if self.build_classifier is not None:
            return self.build_classifier(CHECKPOINT)
        try:
            from tabpfn import TabPFNClassifier
        except ImportError as exc:
            raise ScoringError(
                "tabpfn is not installed. It is an optional extra because it pulls "
                "torch: uv sync --extra prediction"
            ) from exc
        # The checkpoint goes in as a string rather than a `tabpfn.constants`
        # enum member: `ModelVersion` is a str-Enum, so the comparison inside is the
        # same one either way, and this avoids depending on a submodule that is not
        # part of the package's documented surface. If the name is ever retired,
        # `create_default_for_version` raises "Unknown version" - which is the failure
        # we want, loud at construction rather than a silently different model.
        return TabPFNClassifier.create_default_for_version(
            CHECKPOINT,
            random_state=self.seed,
            ignore_pretraining_limits=self.ignore_pretraining_limits,
        )

    def preflight(self) -> str:
        """Prove scoring can happen, before anything depends on it (criterion 18).

        Loads the weights on a two-row problem. Slow and network-bound, and the only
        check that distinguishes a well-formed token from an accepted one.
        """
        check_token()
        frame = pd.DataFrame({"x": [0.0, 1.0, 0.0, 1.0]})
        labels = pd.Series([0, 1, 0, 1])
        try:
            classifier = self._classifier()
            classifier.fit(frame, labels)
            classifier.predict_proba(frame)
        except ScoringError:
            raise
        except Exception as exc:
            raise LicenceRefused(
                f"TabPFN could not load its weights, so churn scoring cannot run: {exc}"
            ) from exc
        return MODEL_VERSION

    def score(
        self,
        *,
        features: pd.DataFrame,
        labels: pd.Series,
        dataset: str,
        data_as_of: str,
    ) -> ChurnScores:
        """Out-of-fold churn probability for every row."""
        check_token()
        if len(features) != len(labels):
            raise ScoringError(f"{len(features)} rows of features, {len(labels)} labels")

        encoded = _encode(features)
        assignment = fold_assignment([int(v) for v in labels], folds=self.folds, seed=self.seed)
        probabilities = [0.0] * len(encoded)

        for fold in range(self.folds):
            held_out = [i for i, f in enumerate(assignment) if f == fold]
            conditioned_on = [i for i, f in enumerate(assignment) if f != fold]
            if not held_out:
                continue
            classifier = self._classifier()
            classifier.fit(encoded.iloc[conditioned_on], labels.take(conditioned_on))
            predicted = classifier.predict_proba(encoded.iloc[held_out])
            for position, row in enumerate(held_out):
                probabilities[row] = float(predicted[position][1])

        return ChurnScores(
            dataset=dataset,
            data_as_of=data_as_of,
            model_version=MODEL_VERSION,
            folds=self.folds,
            seed=self.seed,
            probabilities=tuple(probabilities),
            positives=int(labels.sum()),
        )


def _encode(features: pd.DataFrame) -> pd.DataFrame:
    """Ordinal-encode non-numeric columns, by sorted category.

    Sorted, not by order of appearance: encoding that depends on row order would make
    the same snapshot produce different inputs after a re-sort, and `data_as_of` would
    no longer imply the same scores.
    """
    encoded = features.copy()
    for column in encoded.columns:
        if not pd.api.types.is_numeric_dtype(encoded[column]):
            categories = sorted(str(v) for v in encoded[column].dropna().unique())
            lookup = {value: position for position, value in enumerate(categories)}
            encoded[column] = encoded[column].astype(str).map(lookup).astype("float64")
    return encoded
