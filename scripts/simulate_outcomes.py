"""Plant a treatment effect on real KKBox control outcomes (ADR-0014).

    uv run python scripts/simulate_outcomes.py --variant annual-plan-switch --rollout-on 2017-03-01
    uv run python scripts/simulate_outcomes.py --variant free-month --null   # tau = 0

What is real and what is planted:
- Control: each subscriber's real March 2017 outcome (`is_churn` in the frozen cohort), with
  revenue the real last monthly payment when they renewed.
- Treatment: the same real baseline. A treated subscriber who really churned is flipped to
  renewed with the offer's planted probability. Nobody else changes.
- The effect table and the per-offer costs below are invented, for plumbing, and reviewed at
  Checkpoint L-A. At least one offer loses money so there is something to learn.

The outcomes file carries `simulated: true`. The true tau and true INSV go in a separate
`.truth.json`, which only tests read: the agent reaches outcomes through
`agentstack.context.outcomes`, which never opens it. This lives in scripts/ so the agent
cannot import it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from agentstack.context import datasets
from agentstack.context.assignment import Assignment
from agentstack.context.targeting import Cohort, TargetingRule, select
from agentstack.prediction.churn import RecordedScorer
from agentstack.tools.experiments import VARIANTS

ROOT = Path(__file__).resolve().parents[1]
DATASET = "kkbox-churn"
WINDOW_DAYS = 30

# On the dev KKBox cohort about 73% of the mean payment is at stake per member, so an offer
# pays when 0.73 * lift > cost share. The numbers are set to spread the offers around that line.
# offer -> (chance a treated subscriber who would have churned renews instead,
#           cost per treated subscriber as a share of the cohort's mean monthly payment)
EFFECTS: dict[str, tuple[float, float]] = {
    "10-percent-off": (0.20, 0.10),  # a small win
    "20-percent-off": (0.22, 0.20),  # twice the cost for barely more pull: loses a little
    "free-month": (0.60, 1.00),  # strongest pull, a whole month: loses clearly
    "annual-plan-switch": (0.30, 0.05),  # cheap and sticky: the best
    "pause-subscription": (0.15, 0.02),  # small pull, near free: a modest win
}
assert set(EFFECTS) == set(VARIANTS), "the effect table must cover exactly the menu"


def _draw(seed: int, offer: str, subscriber: int) -> float:
    digest = hashlib.sha256(f"{seed}\x00{offer}\x00{subscriber}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def simulate(
    frame: pd.DataFrame,
    cohort: Cohort,
    assignment: Assignment,
    *,
    offer: str,
    seed: int,
    rollout_on: date,
    null: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The outcomes payload and the planted truth, both as JSON-ready dicts."""
    lift, cost_share = EFFECTS[offer]
    lift = 0.0 if null else lift
    members = cohort.members
    price = (frame["last_actual_amount_paid"].fillna(0) * 100).round().astype("int64")
    prices: list[int] = price.tolist()
    churns: list[bool] = frame["is_churn"].astype(bool).tolist()
    cost = round(cost_share * sum(prices[m] for m in members) / len(members))
    observed_on = (rollout_on + timedelta(days=WINDOW_DAYS)).isoformat()

    arms = assignment.arms()
    rows = []
    # The estimand is the cohort-wide effect, not the effect on whoever was drawn into
    # treatment: randomisation noise in who was drawn is what the interval is for.
    effect_per_member = lift * sum(prices[m] for m in members if churns[m]) / len(members)
    treated = 0
    for m in members:
        churned = churns[m]
        renewed = not churned
        if arms[m] == "treatment":
            treated += 1
            if churned:
                renewed = _draw(seed, offer, m) < lift
        rows.append(
            {
                "subscriber": m,
                "observed_on": observed_on,
                "renewed": renewed,
                "revenue_cents": prices[m] if renewed else 0,
            }
        )

    payload = {
        "experiment_version": cohort.experiment_version,
        "offer": offer,
        "offer_cost_cents": cost,
        "simulated": True,
        "source": f"kkbox control, planted effect (seed {seed}{', null' if null else ''})",
        "rows": rows,
    }
    truth = {
        "simulated": True,
        "seed": seed,
        "offer": offer,
        "lift": lift,
        "treated": treated,
        "true_tau_cents": effect_per_member,
        "true_insv_cents": (effect_per_member - cost) * treated,
    }
    return payload, truth


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--variant", required=True, choices=VARIANTS)
    parser.add_argument("--rollout-on", required=True, type=date.fromisoformat)
    parser.add_argument("--percentage", type=int, default=50)
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--null", action="store_true", help="plant no effect (tau = 0)")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "outcomes")
    args = parser.parse_args()

    snap = datasets.load(DATASET)
    scores = RecordedScorer.load(DATASET).scores
    cohort = select(snap, scores, rule=TargetingRule.load(args.profile))
    payload, truth = simulate(
        snap.frame,
        cohort,
        Assignment(cohort, args.percentage),
        offer=args.variant,
        seed=args.seed,
        rollout_on=args.rollout_on,
        null=args.null,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.variant}-{'null' if args.null else 'planted'}-{args.seed}"
    (args.out / f"{stem}.json").write_text(json.dumps(payload))
    (args.out / f"{stem}.truth.json").write_text(json.dumps(truth, indent=2))
    print(f"wrote {stem}: {truth['treated']} treated of {cohort.size}, truth {truth}")


if __name__ == "__main__":
    main()
