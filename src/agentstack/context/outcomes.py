"""Outcomes after a rollout (layer 5): the one place measured results enter.

The agent reads outcomes only through here, and this refuses what a leak looks like: a
row dated before the rollout (the outcome could have caused the assignment, not the
reverse) and a row for someone outside the frozen cohort (a control drawn from the
general base regresses differently and manufactures a saving).

`simulated` has no default. Today every outcome is semi-synthetic (ADR-0014), and a record
that forgot to say so would read as a result. The planted truth is never read here: the
simulator writes it to a separate file that only tests open.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from agentstack.context.targeting import Cohort


class OutcomesRefused(ValueError):
    """The outcomes file cannot be trusted as a measurement of this rollout."""


@dataclass(frozen=True, slots=True)
class Outcome:
    subscriber: int
    observed_on: date
    renewed: bool
    revenue_cents: int


@dataclass(frozen=True, slots=True)
class Outcomes:
    experiment_version: str
    offer: str
    rollout_on: date
    # Cost per treated subscriber, in cents, recorded when the offer was costed and not
    # estimated here. INSV subtracts it; nothing predicted enters.
    offer_cost_cents: int
    simulated: bool
    source: str
    rows: tuple[Outcome, ...]


def load_outcomes(path: Path, cohort: Cohort, *, rollout_on: date) -> Outcomes:
    """Read outcomes for `cohort`'s rollout, or refuse and say why."""
    raw = json.loads(path.read_text())
    for key in ("experiment_version", "offer", "offer_cost_cents", "simulated", "source", "rows"):
        if key not in raw:
            raise OutcomesRefused(f"{path.name} does not state {key!r}")
    if not isinstance(raw["simulated"], bool):
        raise OutcomesRefused("`simulated` must be true or false, not absent or inferred")
    if raw["experiment_version"] != cohort.experiment_version:
        raise OutcomesRefused(
            f"outcomes are for {raw['experiment_version']}, not {cohort.experiment_version}"
        )

    rows: list[Outcome] = []
    seen: set[int] = set()
    for item in raw["rows"]:
        row = Outcome(
            subscriber=int(item["subscriber"]),
            observed_on=date.fromisoformat(item["observed_on"]),
            renewed=bool(item["renewed"]),
            revenue_cents=int(item["revenue_cents"]),
        )
        if row.observed_on < rollout_on:
            raise OutcomesRefused(
                f"subscriber {row.subscriber} observed {row.observed_on}, before the "
                f"rollout on {rollout_on}"
            )
        if not cohort.includes(row.subscriber):
            raise OutcomesRefused(f"subscriber {row.subscriber} is not in the frozen cohort")
        if row.subscriber in seen:
            raise OutcomesRefused(f"subscriber {row.subscriber} appears twice")
        seen.add(row.subscriber)
        rows.append(row)

    return Outcomes(
        experiment_version=raw["experiment_version"],
        offer=raw["offer"],
        rollout_on=rollout_on,
        offer_cost_cents=int(raw["offer_cost_cents"]),
        simulated=raw["simulated"],
        source=raw["source"],
        rows=tuple(rows),
    )
