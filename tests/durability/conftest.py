"""Shared by the tests that drive triggers through a real worker."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentstack.context.datasets import REGISTRY, CohortSnapshot, DatasetSpec
from tests.fitness.test_trigger_to_candidate import snapshot


@pytest.fixture
def fixture_dataset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The fixture dataset `test_trigger_to_candidate` targets, loaded in-process: the
    test's worker runs its activities in this process, so the monkeypatch reaches them."""
    REGISTRY["fixture"] = DatasetSpec(
        key="fixture",
        kaggle="nobody/nothing",
        files=("fixture.csv",),
        target="Churn",
        churned="1",
        drops={},
        revenue_columns=("monthly charge",),
        revenue_periods_per_year=12,
        revenue_note="fixture revenue, annualised x12",
    )

    def only_the_fixture(key: str, root: Path | None = None) -> CohortSnapshot:
        assert key == "fixture", f"asked for {key!r}"
        assert root is None
        return snapshot()

    monkeypatch.setattr("agentstack.context.datasets.load", only_the_fixture)
    yield
    REGISTRY.pop("fixture", None)
