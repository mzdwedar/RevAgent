"""Learning loop L2 (SPEC criteria 2 and 3): outcomes enter through one door.

The seam refuses rows before the rollout and rows outside the frozen cohort, requires
`simulated` to be stated, and never reads the planted truth. The simulator is checked
against a synthetic frame so CI needs no KKBox download.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agentstack.context.assignment import Assignment
from agentstack.context.outcomes import OutcomesRefused, load_outcomes
from agentstack.context.targeting import Cohort, TargetingRule
from agentstack.tools.experiments import VARIANTS

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "simulate_outcomes", ROOT / "scripts/simulate_outcomes.py"
)
assert _spec and _spec.loader
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

ROLLOUT = date(2017, 3, 1)
N = 2_000
RULE = TargetingRule("test", 0.9, 10, 1, ())


def _frame() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    return pd.DataFrame(
        {"is_churn": rng.random(N) < 0.5, "last_actual_amount_paid": np.full(N, 149.0)}
    )


def _cohort(version: str = "exp:aaaa") -> Cohort:
    return Cohort(
        experiment_version=version,
        dataset="test",
        data_as_of="2017-02-28",
        model_version="m1",
        rule=RULE,
        risk_threshold=0.5,
        members=tuple(range(N)),
        annual_value_at_risk_cents=1,
        risk_quantiles=(0.5,) * 5,
        revenue_note="",
    )


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "outcomes.json"
    path.write_text(json.dumps(payload))
    return path


def _payload(**override: object) -> dict:
    payload, _ = sim.simulate(
        _frame(),
        _cohort(),
        Assignment(_cohort(), 50),
        offer="annual-plan-switch",
        seed=1,
        rollout_on=ROLLOUT,
    )
    return {**payload, **override}


def test_provenance_is_carried(tmp_path: Path) -> None:
    got = load_outcomes(_write(tmp_path, _payload()), _cohort(), rollout_on=ROLLOUT)
    assert got.simulated is True
    assert got.offer == "annual-plan-switch"
    assert got.offer_cost_cents > 0
    assert len(got.rows) == N


def test_simulated_must_be_stated(tmp_path: Path) -> None:
    payload = _payload()
    del payload["simulated"]
    with pytest.raises(OutcomesRefused, match="simulated"):
        load_outcomes(_write(tmp_path, payload), _cohort(), rollout_on=ROLLOUT)


def test_simulated_cannot_be_inferred_from_a_truthy_value(tmp_path: Path) -> None:
    with pytest.raises(OutcomesRefused, match="true or false"):
        load_outcomes(_write(tmp_path, _payload(simulated="yes")), _cohort(), rollout_on=ROLLOUT)


def test_a_row_before_the_rollout_is_refused(tmp_path: Path) -> None:
    payload = _payload()
    payload["rows"][0]["observed_on"] = "2017-02-27"
    with pytest.raises(OutcomesRefused, match="before the rollout"):
        load_outcomes(_write(tmp_path, payload), _cohort(), rollout_on=ROLLOUT)


def test_a_subscriber_outside_the_cohort_is_refused(tmp_path: Path) -> None:
    payload = _payload()
    payload["rows"][0]["subscriber"] = N + 5
    with pytest.raises(OutcomesRefused, match="not in the frozen cohort"):
        load_outcomes(_write(tmp_path, payload), _cohort(), rollout_on=ROLLOUT)


def test_a_duplicate_subscriber_is_refused(tmp_path: Path) -> None:
    payload = _payload()
    payload["rows"][1]["subscriber"] = payload["rows"][0]["subscriber"]
    with pytest.raises(OutcomesRefused, match="twice"):
        load_outcomes(_write(tmp_path, payload), _cohort(), rollout_on=ROLLOUT)


def test_outcomes_for_another_experiment_version_are_refused(tmp_path: Path) -> None:
    with pytest.raises(OutcomesRefused, match="not exp:bbbb"):
        load_outcomes(_write(tmp_path, _payload()), _cohort("exp:bbbb"), rollout_on=ROLLOUT)


def test_the_seam_never_opens_the_planted_truth() -> None:
    source = (ROOT / "src/agentstack/context/outcomes.py").read_text()
    assert ".truth" not in source


def _arm_means(payload: dict) -> tuple[float, float]:
    arms = Assignment(_cohort(), 50).arms()
    by_arm: dict[str, list[bool]] = {"treatment": [], "control": []}
    for row in payload["rows"]:
        by_arm[arms[row["subscriber"]]].append(row["renewed"])
    return float(np.mean(by_arm["treatment"])), float(np.mean(by_arm["control"]))


def test_with_tau_zero_the_arms_match() -> None:
    payload, truth = sim.simulate(
        _frame(),
        _cohort(),
        Assignment(_cohort(), 50),
        offer="free-month",
        seed=3,
        rollout_on=ROLLOUT,
        null=True,
    )
    treated, control = _arm_means(payload)
    assert abs(treated - control) < 0.05
    assert truth["true_tau_cents"] == 0.0


def test_a_planted_effect_moves_only_the_treated_arm() -> None:
    payload, truth = sim.simulate(
        _frame(),
        _cohort(),
        Assignment(_cohort(), 50),
        offer="free-month",
        seed=3,
        rollout_on=ROLLOUT,
    )
    treated, control = _arm_means(payload)
    assert treated - control > 0.2  # 0.6 of the ~50% who would have churned
    assert truth["true_tau_cents"] > 0


def test_the_effect_table_covers_the_menu_and_includes_a_loser_and_a_winner() -> None:
    assert set(sim.EFFECTS) == set(VARIANTS)
    insv = {}
    for offer in VARIANTS:
        _, truth = sim.simulate(
            _frame(),
            _cohort(),
            Assignment(_cohort(), 50),
            offer=offer,
            seed=0,
            rollout_on=ROLLOUT,
        )
        insv[offer] = truth["true_insv_cents"]
    assert min(insv.values()) < 0 < max(insv.values())
    assert len(set(round(v) for v in insv.values())) == len(VARIANTS)  # offers differ
