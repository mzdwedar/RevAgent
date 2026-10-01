"""Who enters the experiment (layers 4-5).

The scorer ranks. This decides. Keeping those apart is the point: a model that also
chose the cohort would be making a business decision inside an inference call, and the
decision would be unreviewable - there would be no threshold to read, no rule to
version, and nothing to re-check a control subject against.

So everything here is deterministic and written down. Given the same snapshot, the
same model version and the same rule, it returns the same customers, in the same
order, with the same threshold (criterion 17). A cohort that cannot be reproduced
cannot be rolled out to.

Three gates, and two of them are refusals rather than adjustments. A cohort that is
too small or too cheap does not get quietly widened until it qualifies - widening is
how the top decile becomes the top third and the targeting stops meaning anything.
"""

from __future__ import annotations

import hashlib
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from agentstack.context.datasets import REGISTRY, CohortSnapshot
from agentstack.prediction.churn import ChurnScores

DEFAULTS = Path(__file__).resolve().parents[3] / "experiments" / "targeting.toml"

# What the cohort's risk profile is summarised by, and recorded on the experiment.
REPORTED_QUANTILES = (0.0, 0.25, 0.5, 0.75, 1.0)


class TargetingRefused(RuntimeError):
    """The cohort does not qualify. Not an error to work around - an answer."""


class RevenueNotObserved(TargetingRefused):
    """This dataset has no observed revenue, so value at risk cannot be computed."""


class CohortTooSmall(TargetingRefused):
    """Fewer eligible customers than the minimum worth experimenting on."""


class NotEnoughAtRisk(TargetingRefused):
    """The eligible cohort is not worth the offers it would take to retain it."""


@dataclass(frozen=True, slots=True)
class TargetingRule:
    """One named set of thresholds.

    Named, because the numbers that suit a tenant's active base do not suit a 3,333-row
    dev dataset, and the difference has to be visible rather than edited in. The name
    travels in `experiment_version` and in the cohort description.
    """

    profile: str
    risk_quantile: float
    minimum_cohort: int
    minimum_annual_value_at_risk_cents: int

    @staticmethod
    def load(profile: str = "default", path: Path = DEFAULTS) -> TargetingRule:
        profiles = tomllib.loads(path.read_text())["profiles"]
        if profile not in profiles:
            raise TargetingRefused(
                f"{profile!r} is not a targeting profile; known: {sorted(profiles)}"
            )
        rule = profiles[profile]
        return TargetingRule(
            profile=profile,
            risk_quantile=float(rule["risk_quantile"]),
            minimum_cohort=int(rule["minimum_cohort"]),
            minimum_annual_value_at_risk_cents=int(rule["minimum_annual_value_at_risk_cents"]),
        )

    def __post_init__(self) -> None:
        if not 0.0 < self.risk_quantile < 1.0:
            raise TargetingRefused(f"risk_quantile {self.risk_quantile} selects everyone or no one")
        if self.minimum_cohort < 1:
            raise TargetingRefused("a cohort of zero is not an experiment")

    def fingerprint(self) -> str:
        return (
            f"{self.profile}"
            f":q{self.risk_quantile}"
            f":n{self.minimum_cohort}"
            f":v{self.minimum_annual_value_at_risk_cents}"
        )


@dataclass(frozen=True, slots=True)
class Cohort:
    """A frozen eligible population, and every choice that produced it."""

    experiment_version: str
    dataset: str
    data_as_of: str
    model_version: str
    rule: TargetingRule
    risk_threshold: float
    members: tuple[int, ...]
    annual_value_at_risk_cents: int
    risk_quantiles: tuple[float, ...]
    revenue_note: str
    currency: str = "USD"

    @property
    def size(self) -> int:
        return len(self.members)

    def includes(self, row: int) -> bool:
        """Re-checkable membership.

        Criterion 11 needs this: every control subject must pass the same predicate at
        the same model version as every treatment subject. A control drawn from the
        general base would regress differently from the targeted arm and manufacture a
        saving that was never there.
        """
        return row in self._membership

    @property
    def _membership(self) -> frozenset[int]:
        return frozenset(self.members)

    def description(self) -> dict[str, object]:
        """What is recorded on the experiment and shown to an approver."""
        described: dict[str, object] = {
            "experiment_version": self.experiment_version,
            "dataset": self.dataset,
            "data_as_of": self.data_as_of,
            "targeting_model_version": self.model_version,
            "risk_threshold": round(self.risk_threshold, 6),
            "targeting_profile": self.rule.profile,
            "rule": self.rule.fingerprint(),
            "size": self.size,
            "annual_value_at_risk_cents": self.annual_value_at_risk_cents,
            "risk_quantiles": {
                str(q): round(v, 6)
                for q, v in zip(REPORTED_QUANTILES, self.risk_quantiles, strict=True)
            },
            "revenue_basis": self.revenue_note,
        }
        # Only a non-USD cohort says so: this record is stored as written, and a USD
        # cohort's must stay byte-identical to what was frozen before currency existed.
        if self.currency != "USD":
            described["currency"] = self.currency
        return described


def money(cents: int, currency: str = "USD") -> str:
    """`$1,000` for USD (unchanged), `NTD 1,000` for anything else."""
    amount = f"{cents / 100:,.0f}"
    return f"${amount}" if currency == "USD" else f"{currency} {amount}"


def annual_revenue_cents(snapshot: CohortSnapshot) -> pd.Series:
    """Observed annualised revenue per customer, in cents.

    Refuses rather than guessing. The value-at-risk floor is specified against observed
    ARPU, so a dataset with no revenue column has no value at risk - it does not have
    one that happens to be zero, and it does not get a modelled substitute.
    """
    spec = REGISTRY[snapshot.dataset]
    if not spec.revenue_columns or spec.revenue_periods_per_year <= 0:
        raise RevenueNotObserved(
            f"{snapshot.dataset} has no observed revenue column, so annualised value at "
            f"risk cannot be computed: {spec.revenue_note}. This cohort can be loaded "
            "and scored; it cannot be targeted against a dollar floor."
        )
    missing = [c for c in spec.revenue_columns if c not in snapshot.frame.columns]
    if missing:
        raise RevenueNotObserved(f"{snapshot.dataset}: revenue columns {missing} are not present")

    per_period = snapshot.frame[list(spec.revenue_columns)].sum(axis=1)
    return (per_period * spec.revenue_periods_per_year * 100).round().astype("int64")


def select(
    snapshot: CohortSnapshot, scores: ChurnScores, *, rule: TargetingRule | None = None
) -> Cohort:
    """Freeze the eligible cohort, or refuse and say which gate stopped it."""
    rule = rule or TargetingRule.load()
    if scores.data_as_of != snapshot.data_as_of:
        raise TargetingRefused(
            f"scores are for {scores.data_as_of}, cohort is {snapshot.data_as_of}; "
            "a population scored against a different snapshot is not this population"
        )
    if len(scores.probabilities) != snapshot.rows:
        raise TargetingRefused(f"{len(scores.probabilities)} scores for {snapshot.rows} customers")

    revenue = annual_revenue_cents(snapshot)
    currency = REGISTRY[snapshot.dataset].currency

    # Rank, then cut at a count. A quantile threshold with ties can return more than a
    # decile, and the number of customers in the cohort is not a detail - it is what
    # the minimum-size gate is about and what the approver is shown.
    take = math.ceil(snapshot.rows * (1.0 - rule.risk_quantile))
    ranked = sorted(range(snapshot.rows), key=lambda i: (-scores.probabilities[i], i))
    members = tuple(sorted(ranked[:take]))
    threshold = min(scores.probabilities[i] for i in members)

    if len(members) < rule.minimum_cohort:
        raise CohortTooSmall(
            f"{len(members)} customers in the top {100 * (1 - rule.risk_quantile):.0f}% of "
            f"{snapshot.rows}; the rule needs {rule.minimum_cohort}. Widening the cut to "
            "qualify would make the targeting mean something else."
        )

    at_risk = int(revenue.take(members).sum())
    if at_risk < rule.minimum_annual_value_at_risk_cents:
        raise NotEnoughAtRisk(
            f"{money(at_risk, currency)} of annualised revenue at risk across {len(members)} "
            f"customers; the rule needs {money(rule.minimum_annual_value_at_risk_cents, currency)}"
        )

    member_risk = sorted(scores.probabilities[i] for i in members)
    quantiles = tuple(
        member_risk[min(len(member_risk) - 1, int(q * (len(member_risk) - 1) + 0.5))]
        for q in REPORTED_QUANTILES
    )

    return Cohort(
        experiment_version=_experiment_version(snapshot, scores, rule, threshold, members),
        dataset=snapshot.dataset,
        data_as_of=snapshot.data_as_of,
        model_version=scores.model_version,
        rule=rule,
        risk_threshold=threshold,
        members=members,
        annual_value_at_risk_cents=at_risk,
        risk_quantiles=quantiles,
        revenue_note=REGISTRY[snapshot.dataset].revenue_note,
        currency=currency,
    )


def _experiment_version(
    snapshot: CohortSnapshot,
    scores: ChurnScores,
    rule: TargetingRule,
    threshold: float,
    members: tuple[int, ...],
) -> str:
    """One version covering every frozen choice, plus the population they produced.

    The membership digest is in here on purpose. Criterion 14 - a rollout cannot exceed
    the cohort - needs the version to bind to *these* customers, not merely to the
    settings that would select them. And because the settings are deterministic, adding
    the members changes nothing about reproducibility.
    """
    digest = hashlib.sha256()
    for part in (
        snapshot.dataset,
        snapshot.data_as_of,
        scores.model_version,
        f"seed:{scores.seed}",
        f"folds:{scores.folds}",
        rule.fingerprint(),
        f"threshold:{threshold:.9f}",
    ):
        digest.update(part.encode())
        digest.update(b"\x00")
    digest.update(",".join(str(m) for m in members).encode())
    return f"exp:{digest.hexdigest()[:16]}"
