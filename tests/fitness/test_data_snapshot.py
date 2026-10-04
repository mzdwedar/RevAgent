"""Context, retrieval, memory: a cohort is identified by what it contains, not by when it was read.

An experiment's effect estimate is only interpretable against the population it was
defined on. `data_as_of` is that population's identity, so it has to be derivable from
the data and from nothing else - otherwise "same snapshot" is a claim nobody can check.

These run without the datasets, on frames built here. The real cohorts are checked in
`tests/live/`, which is declared in CONSTRAINTS.md rather than hidden in a decorator.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pandas as pd
import pytest

from agentstack.context import datasets
from agentstack.context.datasets import DatasetError, DatasetSpec

SPEC = DatasetSpec(
    key="fixture",
    kaggle="nobody/nothing",
    files=("fixture.csv",),
    target="Exited",
    churned="1",
    drops={"Complain": "logged as part of the churn event, so it is an outcome"},
)


def frame(rows: int = 8, **overrides: list[Any]) -> pd.DataFrame:
    columns: dict[str, list[Any]] = {
        "CustomerId": [f"c-{i}" for i in range(rows)],
        "Balance": [100.0 * i for i in range(rows)],
        "Complain": [i % 2 for i in range(rows)],
        "Exited": [str(i % 2) for i in range(rows)],
    }
    return pd.DataFrame(columns | overrides)


def test_the_same_data_yields_the_same_watermark() -> None:
    assert (
        datasets.snapshot(SPEC, frame()).data_as_of == datasets.snapshot(SPEC, frame()).data_as_of
    )


def test_a_single_changed_cell_changes_the_watermark() -> None:
    """The alarm. A cohort that silently moved under an experiment is the worst case."""
    moved = frame()
    moved.loc[0, "Balance"] = 999.0

    assert datasets.snapshot(SPEC, moved).data_as_of != datasets.snapshot(SPEC, frame()).data_as_of


def test_a_different_dataset_with_identical_rows_is_a_different_watermark() -> None:
    other = replace(SPEC, key="other")

    assert (
        datasets.snapshot(SPEC, frame()).data_as_of != datasets.snapshot(other, frame()).data_as_of
    )


def test_the_watermark_is_not_a_timestamp() -> None:
    """Stated as a test because "as of" reads like a clock, and a clock would make the
    reproducibility criterion uncheckable: two runs on one dataset would disagree."""
    first = datasets.snapshot(SPEC, frame()).data_as_of

    assert first == datasets.snapshot(SPEC, frame()).data_as_of
    assert first.startswith("fixture:")


def test_row_order_is_not_part_of_the_identity() -> None:
    """Two reads of one table that differ only in order are the same population."""
    shuffled = frame().iloc[::-1]

    assert datasets.snapshot(SPEC, shuffled).rows == datasets.snapshot(SPEC, frame()).rows


def test_a_documented_leakage_column_is_dropped() -> None:
    snapshot = datasets.snapshot(SPEC, frame())

    assert "Complain" not in snapshot.columns
    assert "Balance" in snapshot.columns


def test_a_documented_column_that_has_vanished_is_an_error_not_a_shrug() -> None:
    """If `Complain` is already gone, the dataset changed shape. Quietly continuing
    would train on a table nobody has re-read."""
    without = frame().drop(columns=["Complain"])

    with pytest.raises(DatasetError, match="Complain"):
        datasets.snapshot(SPEC, without)


def test_a_missing_target_is_an_error() -> None:
    with pytest.raises(DatasetError, match="Exited"):
        datasets.snapshot(SPEC, frame().drop(columns=["Exited"]))


def test_near_identifier_columns_are_dropped() -> None:
    """A column with a distinct value per row names rows; it does not describe them."""
    rows = datasets.MAX_CATEGORICAL_LEVELS + 10
    snapshot = datasets.snapshot(SPEC, frame(rows))

    assert "CustomerId" not in snapshot.columns


def test_a_low_cardinality_category_is_kept() -> None:
    kept = frame(8, Geography=["FR", "DE"] * 4)

    assert "Geography" in datasets.snapshot(SPEC, kept).columns


def test_the_target_becomes_zero_or_one() -> None:
    snapshot = datasets.snapshot(SPEC, frame())

    assert set(snapshot.frame["Exited"].unique()) <= {0, 1}
    assert snapshot.churn_rate == pytest.approx(0.5)


def test_every_dropped_column_says_why_it_is_dropped() -> None:
    """A drop with no reason is indistinguishable from a bug six months later."""
    for spec in datasets.REGISTRY.values():
        for column, reason in spec.drops.items():
            assert len(reason) > 20, f"{spec.key}.{column} is dropped for {reason!r}"


def test_an_unregistered_dataset_is_refused() -> None:
    with pytest.raises(DatasetError, match="not a registered dataset"):
        datasets.load("whatever-we-found-lying-around")


def test_missing_files_name_the_command_that_fetches_them(tmp_path: object) -> None:
    from pathlib import Path

    with pytest.raises(DatasetError, match="fetch_datasets"):
        datasets.load("bank-churn", root=Path(str(tmp_path)))


def test_the_manifest_records_a_watermark_for_every_registered_dataset() -> None:
    """The datasets are third-party and not committed. The manifest is, so a change
    upstream shows up as a diff rather than as a quietly different experiment."""
    manifest = datasets.read_manifest()
    if not manifest:
        pytest.fail(
            "data/manifest.json is missing; run: uv run python scripts/fetch_datasets.py --record"
        )

    # A cohort derived by a build script has no watermark until it is built (K11 records it).
    # That is the only exemption: a downloaded cohort must always be recorded, and anything
    # recorded must be registered and is checked below.
    unbuilt = {key for key, spec in datasets.REGISTRY.items() if spec.derived_by} - set(manifest)
    assert set(manifest) == set(datasets.REGISTRY) - unbuilt
    for key, recorded in manifest.items():
        assert recorded["data_as_of"].startswith(f"{key}:")
        assert recorded["rows"] > 0
        assert 0.0 < recorded["churn_rate"] < 1.0
        assert json.dumps(recorded)  # serialisable, since it travels with the experiment


def test_the_snapshot_summary_is_what_travels_with_an_experiment() -> None:
    """An effect estimate without its population is uninterpretable, so the summary
    goes into `experiment_version` and into the approval prompt."""
    snapshot = datasets.snapshot(SPEC, frame())

    summary = snapshot.summary()

    assert summary["data_as_of"] == snapshot.data_as_of
    assert summary["rows"] == snapshot.rows
    assert summary["dataset"] == "fixture"
    assert json.loads(json.dumps(summary)) == summary


def test_a_documented_fill_replaces_blanks_rather_than_dropping_rows() -> None:
    """`telco` has 11 blank TotalCharges, every one with tenure=0 — signed up, never
    billed. The value is genuinely zero; dropping the rows would bias the cohort."""
    spec = replace(SPEC, fills={"Balance": 0.0})
    blanks = frame()
    blanks.loc[0, "Balance"] = None

    snapshot = datasets.snapshot(spec, blanks)

    assert snapshot.rows == len(frame())
    assert snapshot.frame.loc[0, "Balance"] == 0.0


def test_an_absent_manifest_reads_as_empty_rather_than_raising() -> None:
    """A fresh clone has no manifest yet. That is a fact to report, not a crash."""
    from pathlib import Path

    assert datasets.read_manifest(Path("/nonexistent/manifest.json")) == {}


def test_a_committed_score_file_matches_the_dataset_it_names() -> None:
    """Scores are only evidence about the rows they were computed on.

    The replay scorer refuses a moved snapshot at run time; this refuses it at commit
    time, so a score file cannot sit in the repo describing data that has since changed.
    Only committed files are checked: the others are local by design (see .gitignore).
    """
    import subprocess
    from pathlib import Path

    root = Path(datasets.DATA_ROOT).parent
    tracked = subprocess.run(
        ["git", "ls-files", "data/scores"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()

    for name in tracked:
        recorded = json.loads((root / name).read_text())
        snap = datasets.load(recorded["dataset"])

        assert recorded["data_as_of"] == snap.data_as_of, name
        assert len(recorded["probabilities"]) == snap.rows, name
        assert recorded["positives"] == int(
            snap.frame[datasets.REGISTRY[snap.dataset].target].sum()
        ), name
