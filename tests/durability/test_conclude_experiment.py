"""L5b-1: concluding a measured experiment, against a real database.

The orchestration is plain code, so no Temporal here: what matters is that the cohort is
rebuilt as the same version, the readout is decided by the rule, and an at-least-once call
writes exactly one conclusion.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agentstack.context.assignment import Assignment
from agentstack.context.conclusion_store import ConclusionStore
from agentstack.context.datasets import REGISTRY, CohortSnapshot, DatasetSpec
from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.context.outcomes import OutcomesRefused, outcomes_path
from agentstack.context.targeting import TargetingRule, select
from agentstack.prediction.churn import ChurnScores
from agentstack.runtime.conclude import ConclusionRefused, conclude_experiment
from agentstack.runtime.drafting import ROLLOUT_PERCENTAGE
from agentstack.storage.database import Database, IntegrityViolation

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("sim", ROOT / "scripts/simulate_outcomes.py")
assert _spec and _spec.loader
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

TENANT, EXPERIMENT = "acme", "exp-7"
ROWS = 4_000
ROLLOUT = date(2017, 3, 1)
RULE = TargetingRule("conclude-test", 0.5, 10, 1, ())


def _frame() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    risk = rng.random(ROWS)
    return pd.DataFrame(
        {
            "last_actual_amount_paid": rng.choice([99.0, 149.0, 199.0], ROWS),
            "is_churn": (rng.random(ROWS) < 0.3 + 0.6 * risk).astype(int),
            "risk": risk,
        }
    )


def _snapshot(as_of: str = "concl:1") -> CohortSnapshot:
    frame = _frame()
    return CohortSnapshot("concl", as_of, ROWS, tuple(frame.columns), 0.5, frame)


class Scorer:
    model_version = "stub-1"

    def score(
        self, *, features: pd.DataFrame, labels: pd.Series, dataset: str, data_as_of: str
    ) -> ChurnScores:
        p = tuple(float(v) for v in np.clip(features["risk"].to_numpy(), 0, 1))
        return ChurnScores(dataset, data_as_of, "stub-1", 5, 1, p, int(labels.sum()))


def _loader(as_of: str):
    def load(key: str, root: Path | None = None) -> CohortSnapshot:
        assert key == "concl" and root is None
        return _snapshot(as_of)

    return load


@pytest.fixture(autouse=True)
def concl_dataset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    REGISTRY["concl"] = DatasetSpec(
        key="concl", kaggle="n/a", files=("c.csv",), target="is_churn", churned="1", drops={},
        revenue_columns=("last_actual_amount_paid",), revenue_periods_per_year=12,
        revenue_note="test",
    )  # fmt: skip
    monkeypatch.setattr("agentstack.context.datasets.load", _loader("concl:1"))
    yield
    REGISTRY.pop("concl", None)


def _freeze(db: Database) -> tuple[FrozenCohortStore, str]:
    snap = _snapshot()
    cohort = select(snap, Scorer().score(
        features=snap.frame.drop(columns=["is_churn"]), labels=snap.frame["is_churn"],
        dataset="concl", data_as_of=snap.data_as_of), rule=RULE)  # fmt: skip
    cohorts = FrozenCohortStore(db)
    cohorts.record(tenant=TENANT, experiment_id=EXPERIMENT, cohort=cohort)
    return cohorts, cohort.experiment_version


def _outcomes(tmp_path: Path, offer: str, version: str) -> None:
    snap = _snapshot()
    cohort = select(snap, Scorer().score(
        features=snap.frame.drop(columns=["is_churn"]), labels=snap.frame["is_churn"],
        dataset="concl", data_as_of=snap.data_as_of), rule=RULE)  # fmt: skip
    payload, _ = sim.simulate(
        snap.frame, cohort, Assignment(cohort, ROLLOUT_PERCENTAGE), offer=offer, seed=1,
        rollout_on=ROLLOUT,
    )  # fmt: skip
    outcomes_path(tmp_path, EXPERIMENT, version).write_text(json.dumps(payload))


def _conclude(
    db: Database, cohorts: FrozenCohortStore, version: str, root: Path, endpoint: int = 0
):
    return conclude_experiment(
        cohorts=cohorts, store=ConclusionStore(db), scorer=Scorer(), tenant=TENANT,
        experiment_id=EXPERIMENT, experiment_version=version, rollout_on=ROLLOUT,
        endpoint=endpoint, outcomes_root=root, targeting=RULE,
    )  # fmt: skip


def _count(db: Database) -> int:
    row = db.fetch_one("SELECT count(*) FROM conclusions")
    assert row is not None
    return int(row[0])


def test_a_winning_offer_promotes_and_a_losing_one_stops(
    app_database: Database, tmp_path: Path
) -> None:
    cohorts, version = _freeze(app_database)
    _outcomes(tmp_path, "annual-plan-switch", version)
    won = _conclude(app_database, cohorts, version, tmp_path)
    assert won.action == "promote"
    assert won.simulated is True

    _outcomes(tmp_path, "free-month", version)
    lost = _conclude(app_database, cohorts, version, tmp_path, endpoint=1)
    assert lost.action == "stop"


def test_the_same_endpoint_concludes_once(app_database: Database, tmp_path: Path) -> None:
    cohorts, version = _freeze(app_database)
    _outcomes(tmp_path, "annual-plan-switch", version)
    first = _conclude(app_database, cohorts, version, tmp_path)
    again = _conclude(app_database, cohorts, version, tmp_path)
    assert again == first
    assert _count(app_database) == 1


def test_a_second_call_does_not_read_the_outcomes_again(
    app_database: Database, tmp_path: Path
) -> None:
    cohorts, version = _freeze(app_database)
    _outcomes(tmp_path, "annual-plan-switch", version)
    first = _conclude(app_database, cohorts, version, tmp_path)
    outcomes_path(tmp_path, EXPERIMENT, version).unlink()
    assert _conclude(app_database, cohorts, version, tmp_path) == first


def test_outcomes_that_are_missing_refuse(app_database: Database, tmp_path: Path) -> None:
    cohorts, version = _freeze(app_database)
    with pytest.raises(FileNotFoundError):
        _conclude(app_database, cohorts, version, tmp_path)
    assert _count(app_database) == 0


def test_data_that_moved_since_the_freeze_is_refused(
    app_database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cohorts, version = _freeze(app_database)
    _outcomes(tmp_path, "annual-plan-switch", version)
    monkeypatch.setattr("agentstack.context.datasets.load", _loader("concl:2"))
    with pytest.raises(ConclusionRefused, match="concl:2"):
        _conclude(app_database, cohorts, version, tmp_path)
    assert _count(app_database) == 0


def test_a_version_that_is_not_the_frozen_one_is_refused(
    app_database: Database, tmp_path: Path
) -> None:
    cohorts, _ = _freeze(app_database)
    with pytest.raises(ConclusionRefused, match="no frozen cohort"):
        _conclude(app_database, cohorts, "exp:other", tmp_path)


def test_outcomes_for_another_version_are_refused(app_database: Database, tmp_path: Path) -> None:
    cohorts, version = _freeze(app_database)
    _outcomes(tmp_path, "annual-plan-switch", version)
    path = outcomes_path(tmp_path, EXPERIMENT, version)
    path.write_text(json.dumps({**json.loads(path.read_text()), "experiment_version": "exp:x"}))
    with pytest.raises(OutcomesRefused, match="exp:x"):
        _conclude(app_database, cohorts, version, tmp_path)


def test_a_conclusion_is_never_edited(app_database: Database, tmp_path: Path) -> None:
    cohorts, version = _freeze(app_database)
    _outcomes(tmp_path, "annual-plan-switch", version)
    _conclude(app_database, cohorts, version, tmp_path)
    with pytest.raises(IntegrityViolation) as refused:
        app_database.execute("UPDATE conclusions SET action = 'stop'")
    assert refused.value.constraint == "conclusions_are_final"


def test_the_stored_row_says_simulated_and_records_the_rule(
    app_database: Database, tmp_path: Path
) -> None:
    cohorts, version = _freeze(app_database)
    _outcomes(tmp_path, "annual-plan-switch", version)
    got = _conclude(app_database, cohorts, version, tmp_path)
    assert got.readout["simulated"] is True
    assert got.rule["max_extensions"] == 1
    assert got.endpoint == 0
