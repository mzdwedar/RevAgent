"""The real cohorts, checked against the recorded manifest.

Not part of the default suite: these need the Kaggle datasets on disk, and CI has no
credentials. The lane is a directory and a row in CONSTRAINTS.md, not a decorator -
a conditional skip would trip our own floor, and the fix for that is not to loosen it.

    uv run pytest tests/live -v
"""

from __future__ import annotations

import pytest

from agentstack.context import datasets

KEYS = sorted(datasets.REGISTRY)


@pytest.fixture(scope="module")
def manifest() -> dict[str, dict[str, object]]:
    recorded = datasets.read_manifest()
    if not recorded:
        pytest.fail("no data/manifest.json; run scripts/fetch_datasets.py --record")
    return recorded


@pytest.mark.parametrize("key", KEYS)
def test_the_cohort_on_disk_is_the_cohort_that_was_recorded(
    key: str, manifest: dict[str, dict[str, object]]
) -> None:
    """A changed watermark means the population moved. That is never routine."""
    snapshot = datasets.load(key)
    recorded = manifest[key]

    assert snapshot.data_as_of == recorded["data_as_of"], (
        f"{key} no longer hashes to its recorded watermark: the data changed. "
        "Re-read the registry before anything is trained on it, then re-record."
    )
    assert snapshot.rows == recorded["rows"]
    assert list(snapshot.columns) == recorded["columns"]


@pytest.mark.parametrize("key", KEYS)
def test_loading_twice_gives_one_answer(key: str) -> None:
    assert datasets.load(key).data_as_of == datasets.load(key).data_as_of


@pytest.mark.parametrize("key", KEYS)
def test_no_surviving_feature_gives_the_answer_away(key: str) -> None:
    """The documented drops handle the leakage we know about. This is the check for
    the leakage we do not - a new column added upstream that encodes the outcome.

    `Complain` sat at r=0.996 and took every model to ~0.999 AUC. A feature above 0.9
    is not a predictor; it is the label wearing a different name.
    """
    snapshot = datasets.load(key)
    target = datasets.REGISTRY[key].target
    numeric = snapshot.frame.select_dtypes("number")

    correlations = numeric.corr()[target].drop(target).abs()
    suspicious = correlations[correlations > 0.9]

    assert suspicious.empty, f"{key}: {dict(suspicious)} correlate with {target} like an outcome"


@pytest.mark.parametrize("key", KEYS)
def test_the_cohort_is_large_enough_to_target_from(key: str) -> None:
    """T7 cuts the top decile and needs at least 1,000 customers in it."""
    snapshot = datasets.load(key)

    assert snapshot.rows >= 1000
    assert 0.05 < snapshot.churn_rate < 0.5, "an extreme base rate makes uplift unmeasurable"
