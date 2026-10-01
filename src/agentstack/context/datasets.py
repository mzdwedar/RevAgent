"""Cohort snapshots: the population an experiment is defined against (Context, retrieval, memory).

An experiment's result is only interpretable against the data it was defined on. So a
snapshot carries a **watermark** - `data_as_of` - and that watermark is derived from
the content, not from the clock.

A timestamp records when someone looked. Two runs on identical data would get two
different values, and the reproducibility criterion ("same snapshot and same model
version produce the same cohort") could never be checked. A content hash records what
was looked at: identical data gives an identical watermark, and a single changed cell
gives a different one, which is the alarm you actually want.

This module reads local files and nothing else. Fetching is `scripts/fetch_datasets.py`,
outside the package, because fetching is network work and only `agentstack.execution`
touches the network.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.api.types import is_numeric_dtype

DATA_ROOT = Path(__file__).resolve().parents[3] / "data"
MANIFEST = Path(__file__).resolve().parents[3] / "data" / "manifest.json"

# A categorical with more levels than this is a near-identifier: it names rows rather
# than describing them, and a model that keys on it has memorised, not learned.
MAX_CATEGORICAL_LEVELS = 100


class DatasetError(RuntimeError):
    """The data on disk is not the data the registry describes."""


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One cohort source, and everything that must be true of it.

    `drops` maps a column to *why* it goes. A dropped column with no reason is
    indistinguishable from a bug, and six months later nobody can tell whether it was
    leakage, noise, or an accident.
    """

    key: str
    kaggle: str
    files: tuple[str, ...]
    target: str
    churned: str
    drops: Mapping[str, str]
    fills: Mapping[str, Any] = field(default_factory=dict)

    # Observed periodic revenue per customer, in the units the file uses. Empty when
    # the dataset has none: the value-at-risk floor is specified against *observed*
    # ARPU, and a dataset without it is loadable but not targetable. Inventing a
    # margin assumption to fill the gap would make the dollar figure a prediction,
    # which is the one thing that number is not allowed to be.
    revenue_columns: tuple[str, ...] = ()
    revenue_periods_per_year: int = 0
    revenue_note: str = ""


REGISTRY: dict[str, DatasetSpec] = {
    "telecom-bigml": DatasetSpec(
        key="telecom-bigml",
        kaggle="mnassrib/telecom-churn-datasets",
        files=("churn-bigml-80.csv", "churn-bigml-20.csv"),
        target="Churn",
        churned="True",
        drops={},
        revenue_columns=(
            "Total day charge",
            "Total eve charge",
            "Total night charge",
            "Total intl charge",
        ),
        revenue_periods_per_year=12,
        revenue_note=(
            "The four charge columns are read as one billing month and annualised x12. "
            "The published dataset does not state the period; this is an assumption, "
            "recorded here rather than buried in a multiplier, because every "
            "value-at-risk figure in an approval prompt depends on it."
        ),
    ),
    "bank-churn": DatasetSpec(
        key="bank-churn",
        kaggle="radheshyamkollipara/bank-customer-churn",
        files=("Customer-Churn-Records.csv",),
        target="Exited",
        churned="1",
        drops={
            "Complain": (
                "correlates with the target at r=0.996 - the complaint is logged as "
                "part of the churn event, so it is an outcome, not a predictor. Left "
                "in, every model scores ~0.999 AUC and the benchmark means nothing."
            ),
            "RowNumber": (
                "the row's position in the file, which carries no information about "
                "the customer and changes if the export is re-sorted"
            ),
            "CustomerId": (
                "an identifier: it names the row rather than describing it, so a model "
                "that keys on it has memorised the training set"
            ),
            "Surname": (
                "an identifier, and a proxy for ethnicity and national origin - a "
                "retention offer must not be targeted on it, however predictive it is"
            ),
        },
        # No revenue columns, deliberately. `Balance` is the customer's deposit,
        # `EstimatedSalary` is their income and `Point Earned` is loyalty points -
        # none is revenue to the bank. Turning a balance into revenue needs a net
        # interest margin, which is a modelling assumption, and the floor is specified
        # against observed ARPU. So this cohort loads and cannot be targeted.
        revenue_note="no observed revenue column; see the comment above",
    ),
}


@dataclass(frozen=True, slots=True)
class CohortSnapshot:
    """A frozen population, identified by what it contains."""

    dataset: str
    data_as_of: str
    rows: int
    columns: tuple[str, ...]
    churn_rate: float
    frame: pd.DataFrame = field(compare=False, repr=False)

    def summary(self) -> dict[str, Any]:
        """What goes in the manifest, and into an experiment's provenance."""
        return {
            "dataset": self.dataset,
            "data_as_of": self.data_as_of,
            "rows": self.rows,
            "columns": list(self.columns),
            "churn_rate": round(self.churn_rate, 6),
        }


def watermark(dataset: str, frame: pd.DataFrame) -> str:
    """A content address for this exact table.

    Hashes the CSV rendering rather than `pd.util.hash_pandas_object`: the latter is
    an internal hash whose value is free to change between pandas releases, and a
    `data_as_of` that moves when a dependency is upgraded is not a watermark.
    """
    digest = hashlib.sha256()
    digest.update(dataset.encode())
    digest.update(b"\x00")
    digest.update(frame.to_csv(index=False).encode())
    return f"{dataset}:{digest.hexdigest()[:16]}"


def clean(spec: DatasetSpec, frame: pd.DataFrame) -> pd.DataFrame:
    """Apply the registry's documented handling, and fail if it no longer applies.

    A documented drop that is already absent is not a convenience - it means the
    upstream data changed shape, and every downstream assumption should be re-read
    before anything is trained on it.
    """
    missing = [column for column in spec.drops if column not in frame.columns]
    if missing:
        raise DatasetError(
            f"{spec.key}: documented columns {missing} are not in the data. "
            "The dataset changed; re-read the registry before trusting a cohort from it."
        )
    if spec.target not in frame.columns:
        raise DatasetError(f"{spec.key}: target column {spec.target!r} is not in the data")

    cleaned = frame.drop(columns=list(spec.drops))
    for column, value in spec.fills.items():
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce").fillna(value)

    # `dtype == object` looks right and is wrong: pandas infers a dedicated string
    # dtype now, so that comparison is False for exactly the columns this is meant to
    # catch. Asking "is it not numeric" is the question that was always intended.
    identifiers = [
        column
        for column in cleaned.columns
        if column != spec.target
        and not is_numeric_dtype(cleaned[column])
        and cleaned[column].nunique() > MAX_CATEGORICAL_LEVELS
    ]
    cleaned = cleaned.drop(columns=identifiers)

    cleaned[spec.target] = (cleaned[spec.target].astype(str) == spec.churned).astype(int)
    return cleaned.sort_index(axis=1).reset_index(drop=True)


def load(key: str, *, root: Path = DATA_ROOT) -> CohortSnapshot:
    """Read, clean and watermark one cohort."""
    spec = REGISTRY.get(key)
    if spec is None:
        raise DatasetError(f"{key!r} is not a registered dataset; known: {sorted(REGISTRY)}")

    paths = [root / name for name in spec.files]
    absent = [p.name for p in paths if not p.exists()]
    if absent:
        raise DatasetError(
            f"{key}: {absent} not found under {root}. Fetch them: uv run python "
            f"scripts/fetch_datasets.py"
        )

    frame = pd.concat([pd.read_csv(path) for path in paths], ignore_index=True)
    return snapshot(spec, frame)


def snapshot(spec: DatasetSpec, frame: pd.DataFrame) -> CohortSnapshot:
    cleaned = clean(spec, frame)
    return CohortSnapshot(
        dataset=spec.key,
        data_as_of=watermark(spec.key, cleaned),
        rows=len(cleaned),
        columns=tuple(cleaned.columns),
        churn_rate=float(cleaned[spec.target].mean()),
        frame=cleaned,
    )


def read_manifest(path: Path = MANIFEST) -> dict[str, dict[str, Any]]:
    """The recorded shape of each cohort.

    Committed, while the datasets themselves are not: they are third-party and
    licensed. The manifest is what makes a change upstream visible as a diff - a new
    `data_as_of` in a pull request is the alarm.
    """
    if not path.exists():
        return {}
    loaded: dict[str, dict[str, Any]] = json.loads(path.read_text())
    return loaded
