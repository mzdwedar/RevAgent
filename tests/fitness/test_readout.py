"""Learning loop L4 (SPEC criteria 5 and 6): the readout is calibrated against a known truth.

The simulator plants tau, so these are the tests that can fail: coverage of the planted
effect, the false-positive rate at tau = 0, and the adjustment doing its job. 100 seeds,
each with its own experiment version and so its own random split.
"""

from __future__ import annotations

import importlib.util
from dataclasses import fields
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agentstack.context.assignment import Assignment
from agentstack.context.outcomes import Outcome, Outcomes
from agentstack.context.readout import Readout, ReadoutRefused, readout
from agentstack.context.targeting import Cohort, TargetingRule
from agentstack.prediction.covariate import Covariate

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("sim", ROOT / "scripts/simulate_outcomes.py")
assert _spec and _spec.loader
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

ROLLOUT = date(2017, 3, 1)
N = 2_000
SEEDS = range(100)
RULE = TargetingRule("test", 0.9, 10, 1, ())


def _world() -> tuple[pd.DataFrame, Covariate]:
    """A population where risk and price drive churn, and the covariate sees both."""
    rng = np.random.default_rng(11)
    risk = rng.random(N)
    price = rng.choice([99.0, 149.0, 199.0, 299.0], N)
    frame = pd.DataFrame(
        {"is_churn": rng.random(N) < 0.2 + 0.6 * risk, "last_actual_amount_paid": price}
    )
    p = np.clip(0.2 + 0.6 * risk + rng.normal(0, 0.1, N), 0, 1)
    values = tuple(float(v) for v in (1 - p) * price * 100)
    return frame, Covariate("test", "ds:1", "m", "(1 - p) * pay", values)


def _cohort(seed: int) -> Cohort:
    return Cohort(
        experiment_version=f"exp:{seed:04d}",
        dataset="test",
        data_as_of="ds:1",
        model_version="m",
        rule=RULE,
        risk_threshold=0.5,
        members=tuple(range(N)),
        annual_value_at_risk_cents=1,
        risk_quantiles=(0.5,) * 5,
        revenue_note="",
    )


def _run(seed: int, *, null: bool, offer: str = "annual-plan-switch") -> tuple[Readout, dict]:
    frame, covariate = _world()
    cohort = _cohort(seed)
    assignment = Assignment(cohort, 50)
    payload, truth = sim.simulate(
        frame, cohort, assignment, offer=offer, seed=seed, rollout_on=ROLLOUT, null=null
    )
    outcomes = Outcomes(
        experiment_version=payload["experiment_version"],
        offer=offer,
        rollout_on=ROLLOUT,
        offer_cost_cents=payload["offer_cost_cents"],
        simulated=payload["simulated"],
        source=payload["source"],
        rows=tuple(
            Outcome(
                r["subscriber"],
                date.fromisoformat(r["observed_on"]),
                r["renewed"],
                r["revenue_cents"],
            )
            for r in payload["rows"]
        ),
    )
    return readout(outcomes, assignment, covariate), truth


@pytest.fixture(scope="module")
def planted() -> list[tuple[Readout, dict]]:
    return [_run(s, null=False) for s in SEEDS]


@pytest.fixture(scope="module")
def null() -> list[tuple[Readout, dict]]:
    return [_run(s, null=True) for s in SEEDS]


def test_the_interval_covers_the_planted_effect(planted: list[tuple[Readout, dict]]) -> None:
    covered = sum(r.adjusted.lower <= t["true_tau_cents"] <= r.adjusted.upper for r, t in planted)
    assert covered >= 92, f"covered {covered}/100"


def test_the_false_positive_rate_at_tau_zero(null: list[tuple[Readout, dict]]) -> None:
    false_positives = sum(not (r.adjusted.lower <= 0 <= r.adjusted.upper) for r, _ in null)
    assert false_positives <= 8, f"{false_positives}/100"


def test_the_adjusted_interval_is_no_wider_than_the_unadjusted(
    planted: list[tuple[Readout, dict]],
) -> None:
    adjusted = np.mean([r.adjusted.se_cents for r, _ in planted])
    unadjusted = np.mean([r.unadjusted.se_cents for r, _ in planted])
    assert adjusted <= unadjusted
    assert np.mean([r.variance_ratio for r, _ in planted]) < 1


def test_insv_is_the_effect_less_the_cost_times_the_treated(
    planted: list[tuple[Readout, dict]],
) -> None:
    r, _ = planted[0]
    assert r.insv_cents == pytest.approx((r.adjusted.tau_cents - r.offer_cost_cents) * r.treated)
    assert r.insv_lower_cents < r.insv_cents < r.insv_upper_cents


def test_a_losing_offer_has_an_interval_below_zero() -> None:
    r, truth = _run(1, null=False, offer="free-month")
    assert truth["true_insv_cents"] < 0
    assert r.insv_upper_cents < 0


def test_insv_reads_only_observed_columns() -> None:
    assert {f.name for f in fields(Outcome)} == {
        "subscriber",
        "observed_on",
        "renewed",
        "revenue_cents",
    }
    assert {f.name for f in fields(Outcomes)} >= {"offer_cost_cents", "simulated"}


def test_simulated_travels_into_the_readout(planted: list[tuple[Readout, dict]]) -> None:
    assert all(r.simulated is True for r, _ in planted)


def test_a_constant_covariate_is_refused() -> None:
    frame, covariate = _world()
    flat = Covariate("test", "ds:1", "m", "flat", (1.0,) * N)
    cohort = _cohort(0)
    payload, _ = sim.simulate(
        frame, cohort, Assignment(cohort, 50), offer="free-month", seed=0, rollout_on=ROLLOUT
    )
    outcomes = Outcomes(
        payload["experiment_version"],
        "free-month",
        ROLLOUT,
        payload["offer_cost_cents"],
        True,
        "t",
        tuple(
            Outcome(r["subscriber"], ROLLOUT, r["renewed"], r["revenue_cents"])
            for r in payload["rows"]
        ),
    )
    with pytest.raises(ReadoutRefused, match="does not vary"):
        readout(outcomes, Assignment(cohort, 50), flat)
