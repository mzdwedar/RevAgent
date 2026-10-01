"""Shared by the tests that drive triggers through a real worker."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentstack.context.datasets import REGISTRY, CohortSnapshot, DatasetSpec
from agentstack.interfaces.wiring import build_stack
from agentstack.storage.database import Database
from tests.fitness.test_trigger_to_candidate import snapshot
from tests.temporal_support import run_for


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


@pytest.fixture
def run_id(app_database: Database, checkpointer: Any) -> str:
    """The recorded run an `acme` trigger for `exp-7` belongs to."""
    return run_for(build_stack(app_database, checkpointer, tenant="acme"))
