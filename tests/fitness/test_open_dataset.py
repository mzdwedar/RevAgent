"""A dataset committed to the repo declares the terms it is committed under.

Third-party datasets stay gitignored because their licences forbid redistribution. A file
under `data/open/` is the exception, and the exception has to be earned: the spec names
the licence and who to credit, and the file is really there, so a clone reproduces the
evidence without a Kaggle account.
"""

from __future__ import annotations

import pytest

from agentstack.context import datasets, targeting

COMMITTED = sorted(
    key
    for key, spec in datasets.REGISTRY.items()
    if any(name.startswith("open/") for name in spec.files)
)


def test_the_netflix_cohort_is_committed() -> None:
    assert "netflix-churn" in COMMITTED


@pytest.mark.parametrize("key", COMMITTED)
def test_a_committed_dataset_states_its_licence_and_attribution(key: str) -> None:
    spec = datasets.REGISTRY[key]

    assert spec.licence, f"{key}: committed data with no stated licence"
    assert spec.attribution, f"{key}: committed data with nobody to credit"


@pytest.mark.parametrize("key", COMMITTED)
def test_a_committed_dataset_loads_from_a_clone(key: str) -> None:
    snapshot = datasets.load(key)

    assert snapshot.rows > 0
    assert 0 < snapshot.churn_rate < 1
    assert (datasets.DATA_ROOT / "open" / "NOTICE.md").exists()


def test_netflix_revenue_is_the_observed_monthly_fee_annualised() -> None:
    snapshot = datasets.load("netflix-churn")

    cents = targeting.annual_revenue_cents(snapshot)

    assert snapshot.rows == 5000
    assert set(cents.unique()) == {round(fee * 12 * 100) for fee in (8.99, 13.99, 17.99)}
    assert "customer_id" not in snapshot.frame.columns
