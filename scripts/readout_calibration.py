"""Run the readout over 100 seeds per offer on the real KKBox cohort; commit the table.

    uv run python scripts/readout_calibration.py

Writes docs/evidence/readout_simulation.csv. Every row is `simulated` (ADR-0014): the
control arm is real, the effect is planted, and the planted truth is in the row so
coverage and false positives can be recounted by anyone. Each seed gets its own experiment
version and so its own random split.
"""

from __future__ import annotations

import csv
import importlib.util
from dataclasses import replace
from datetime import date
from pathlib import Path

from agentstack.context import datasets
from agentstack.context.assignment import Assignment
from agentstack.context.outcomes import Outcome, Outcomes
from agentstack.context.readout import readout
from agentstack.context.targeting import TargetingRule, select
from agentstack.prediction.churn import RecordedScorer
from agentstack.prediction.covariate import eltv_pre
from agentstack.tools.experiments import VARIANTS

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "evidence" / "readout_simulation.csv"
ROLLOUT = date(2017, 3, 1)
SEEDS = range(100)
FIELDS = (
    "offer", "null", "seed", "simulated", "treated", "true_tau", "tau_hat", "lower", "upper",
    "covers", "se_adjusted", "se_unadjusted", "variance_ratio", "cost", "true_insv", "insv",
    "insv_lower", "insv_upper",
)  # fmt: skip

_spec = importlib.util.spec_from_file_location("sim", ROOT / "scripts" / "simulate_outcomes.py")
assert _spec and _spec.loader
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)


def main() -> None:
    snap = datasets.load("kkbox-churn")
    scores = RecordedScorer.load("kkbox-churn").scores
    base = select(snap, scores, rule=TargetingRule.load("dev"))
    pay = (snap.frame["last_actual_amount_paid"].fillna(0) * 100).round().astype("int64")
    covariate = eltv_pre(scores, pay.tolist(), data_as_of=snap.data_as_of)

    rows = []
    for offer in VARIANTS:
        for null in (False, True):
            for seed in SEEDS:
                cohort = replace(base, experiment_version=f"{base.experiment_version}:{seed}")
                assignment = Assignment(cohort, 50)
                payload, truth = sim.simulate(
                    snap.frame, cohort, assignment, offer=offer, seed=seed,
                    rollout_on=ROLLOUT, null=null,
                )  # fmt: skip
                outcomes = Outcomes(
                    cohort.experiment_version, offer, ROLLOUT, payload["offer_cost_cents"],
                    True, payload["source"],
                    tuple(
                        Outcome(r["subscriber"], date.fromisoformat(r["observed_on"]),
                                r["renewed"], r["revenue_cents"])
                        for r in payload["rows"]
                    ),
                )  # fmt: skip
                r = readout(outcomes, assignment, covariate)
                rows.append({
                    "offer": offer, "null": null, "seed": seed, "simulated": True,
                    "treated": r.treated, "true_tau": round(truth["true_tau_cents"], 2),
                    "tau_hat": round(r.adjusted.tau_cents, 2), "lower": round(r.adjusted.lower, 2),
                    "upper": round(r.adjusted.upper, 2),
                    "covers": r.adjusted.lower <= truth["true_tau_cents"] <= r.adjusted.upper,
                    "se_adjusted": round(r.adjusted.se_cents, 2),
                    "se_unadjusted": round(r.unadjusted.se_cents, 2),
                    "variance_ratio": round(r.variance_ratio, 4), "cost": r.offer_cost_cents,
                    "true_insv": round(truth["true_insv_cents"], 2),
                    "insv": round(r.insv_cents, 2), "insv_lower": round(r.insv_lower_cents, 2),
                    "insv_upper": round(r.insv_upper_cents, 2),
                })  # fmt: skip
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
