"""Bounded discretion: what the model may choose, and what that choice costs (ADR-0015).

The model proposes an offer and a rollout percentage. The bounds on both are policy and are
held here, in layer 8, loaded from `experiments/offers.toml` and nowhere else. `Bounds.load`
takes a profile name and a path; there is no parameter through which retrieved text, a
memory item or a prompt could move a bound, and a test holds the signature to that.

A check returns a reason string, or `None` when the value is inside the bound. Admission
quotes the reason to the person who is asked, so it says which bound and what it is.

Cost is computed from *observed* ARPU, in integer cents of the cohort's own currency, and is
rounded up: a cost that rounds down into the cap is a cap that does not hold. An unknown
kind, a missing ARPU or a missing currency is refused, never defaulted.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[3] / "experiments" / "offers.toml"

BPS = 10_000
MONTHS_PER_YEAR = 12

COST_BASES = ("percent_of_arpu_per_month", "months_of_arpu", "uplift_of_arpu_per_month")


class OfferRefused(RuntimeError):
    """The offer, the bounds or the inputs to its cost are not usable. An answer, not a bug."""


@dataclass(frozen=True, slots=True)
class OfferKind:
    """One kind of offer: the range the model may choose from, and how it is costed."""

    amount_min: int
    amount_max: int
    months_min: int
    months_max: int
    cost_basis: str
    uplift_bps: int = 0


@dataclass(frozen=True, slots=True)
class Bounds:
    """Everything the model may not exceed, for one named profile."""

    profile: str
    percentage_min: int
    percentage_max: int
    minimum_treatment_arm: int
    cost_cap_bps: int
    kinds: dict[str, OfferKind]

    @staticmethod
    def load(profile: str = "default", path: Path = DEFAULTS) -> Bounds:
        profiles = tomllib.loads(path.read_text())["profiles"]
        if profile not in profiles:
            raise OfferRefused(f"{profile!r} is not an offers profile; known: {sorted(profiles)}")
        rule = profiles[profile]
        scalars = {
            key: _int(profile, rule, key)
            for key in ("percentage_min", "percentage_max", "minimum_treatment_arm", "cost_cap_bps")
        }
        kinds = {name: _kind(profile, name, spec) for name, spec in rule.get("kinds", {}).items()}
        if not kinds:
            raise OfferRefused(f"offers profile {profile!r} defines no offer kind")
        bounds = Bounds(profile=profile, kinds=kinds, **scalars)
        if not 0 < bounds.percentage_min <= bounds.percentage_max <= 100:
            raise OfferRefused(
                f"offers profile {profile!r}: percentage range "
                f"{bounds.percentage_min}-{bounds.percentage_max} is not within 1-100"
            )
        if bounds.minimum_treatment_arm < 1:
            raise OfferRefused(f"offers profile {profile!r}: an arm of zero measures nothing")
        return bounds

    def check_offer(self, kind: str, amount: int, months: int) -> str | None:
        """A reason the offer is out of bounds, or `None`."""
        spec = self.kinds.get(kind)
        if spec is None:
            return f"offer kind {kind!r} is not in the catalog; allowed: {sorted(self.kinds)}"
        if not spec.amount_min <= amount <= spec.amount_max:
            return (
                f"{kind} amount {amount} is outside {spec.amount_min}-{spec.amount_max} "
                f"allowed for {kind}"
            )
        if not spec.months_min <= months <= spec.months_max:
            return (
                f"{kind} duration {months} month(s) is outside "
                f"{spec.months_min}-{spec.months_max} allowed for {kind}"
            )
        return None

    def check_percentage(self, percentage: int, cohort_size: int) -> str | None:
        """A reason the rollout percentage is out of bounds, or `None`.

        Two bounds apply and both are quoted: the range, and the smallest arm worth
        measuring. The second can be the tighter one.
        """
        if not self.percentage_min <= percentage <= self.percentage_max:
            return (
                f"rollout percentage {percentage} is outside "
                f"{self.percentage_min}-{self.percentage_max}"
            )
        arm = treatment_arm(percentage, cohort_size)
        if arm < self.minimum_treatment_arm:
            return (
                f"rollout percentage {percentage} of {cohort_size} customers is an arm of "
                f"{arm}, under the minimum of {self.minimum_treatment_arm}"
            )
        return None

    def allowed_percentages(self, cohort_size: int) -> tuple[int, int]:
        """The lowest and highest rollout percentage that passes `check_percentage`.

        The range in config, with its low end raised when the arm floor needs more than
        that share of this cohort. Where nothing passes, the configured range: the person
        is shown the bounds, and admission refuses every choice in them.
        """
        allowed = [
            p
            for p in range(self.percentage_min, self.percentage_max + 1)
            if self.check_percentage(p, cohort_size) is None
        ]
        return (allowed[0], allowed[-1]) if allowed else (self.percentage_min, self.percentage_max)

    def check_cost(self, total_cost_cents: int, annual_value_at_risk_cents: int) -> str | None:
        """A reason the total offer cost is over the cap, or `None`."""
        cap_pct = f"{self.cost_cap_bps / 100:g}%"
        if annual_value_at_risk_cents <= 0:
            return f"no annual value at risk to hold the {cap_pct} cost cap against"
        if total_cost_cents * BPS > annual_value_at_risk_cents * self.cost_cap_bps:
            return (
                f"total offer cost {total_cost_cents} exceeds the cap of {cap_pct} of the "
                f"{annual_value_at_risk_cents} annual value at risk"
            )
        return None


def treatment_arm(percentage: int, cohort_size: int) -> int:
    """Customers treated at `percentage` of the cohort, rounded down: never more than asked."""
    return cohort_size * percentage // 100


def offer_cost(
    bounds: Bounds,
    kind: str,
    amount: int,
    months: int,
    observed_annual_arpu_cents: int,
    currency: str,
) -> int:
    """Cost of one offer to one treated customer, in cents of `currency`, rounded up.

    Pure: the same inputs give the same cost. `observed_annual_arpu_cents` is the cohort's
    annual value at risk divided by its size - observed revenue, never a modelled one.
    """
    if not currency:
        raise OfferRefused("an offer cost needs the cohort's currency; none was given")
    if observed_annual_arpu_cents <= 0:
        raise OfferRefused(
            f"an offer cost needs observed ARPU above zero; got {observed_annual_arpu_cents}"
        )
    reason = bounds.check_offer(kind, amount, months)
    if reason is not None:
        raise OfferRefused(reason)
    spec = bounds.kinds[kind]
    arpu = observed_annual_arpu_cents
    if spec.cost_basis == "percent_of_arpu_per_month":
        return _ceil_div(arpu * amount * months, 100 * MONTHS_PER_YEAR)
    if spec.cost_basis == "months_of_arpu":
        return _ceil_div(arpu * amount, MONTHS_PER_YEAR)
    return _ceil_div(arpu * spec.uplift_bps * months, BPS * MONTHS_PER_YEAR)


def describe_offer(kind: str, amount: int, months: int) -> str:
    """The offer in words a person weighs: what the approver is shown, not what is stored."""
    span = f"{months} month{'s' if months != 1 else ''}"
    if kind == "percent_off":
        return f"{amount}% off for {span}"
    if kind == "free_months":
        return f"{amount} free month{'s' if amount != 1 else ''}"
    if kind == "tier_upgrade":
        return f"tier upgrade for {span}"
    return f"{kind} ({amount}) for {span}"


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _int(where: str, table: dict[str, object], key: str, *, default: int | None = None) -> int:
    value = table.get(key, default)
    if value is None:
        raise OfferRefused(f"offers profile {where!r} states no {key}; none is assumed")
    if isinstance(value, bool) or not isinstance(value, int):
        raise OfferRefused(f"offers profile {where!r}: {key} must be a whole number")
    return value


def _kind(profile: str, name: str, spec: dict[str, object]) -> OfferKind:
    where = f"{profile}.kinds.{name}"
    basis = spec.get("cost_basis")
    if basis not in COST_BASES:
        raise OfferRefused(f"offers {where}: cost_basis must be one of {list(COST_BASES)}")
    kind = OfferKind(
        amount_min=_int(where, spec, "amount_min"),
        amount_max=_int(where, spec, "amount_max"),
        months_min=_int(where, spec, "months_min"),
        months_max=_int(where, spec, "months_max"),
        cost_basis=str(basis),
        uplift_bps=_int(where, spec, "uplift_bps", default=0),
    )
    if kind.cost_basis == "uplift_of_arpu_per_month" and kind.uplift_bps <= 0:
        raise OfferRefused(f"offers {where}: an uplift offer states uplift_bps; none is assumed")
    if not 0 < kind.amount_min <= kind.amount_max or not 0 < kind.months_min <= kind.months_max:
        raise OfferRefused(f"offers {where}: ranges must be positive and min <= max")
    return kind


def draft_cost(
    bounds: Bounds,
    kind: str,
    amount: int,
    months: int,
    *,
    cohort_size: int,
    annual_value_at_risk_cents: int,
    currency: str,
) -> tuple[int, int]:
    """(cost per customer, total cost) of an offer, in cents, or `OfferRefused`.

    The total is over the widest arm policy allows, because the percentage is chosen
    later and the cap must hold for whatever it is. Observed ARPU is the cohort's value
    at risk over its size: nothing here is a number the model supplied.
    """
    if cohort_size <= 0:
        raise OfferRefused("a cohort with no members has no cost to cap")
    per_customer = offer_cost(
        bounds, kind, amount, months, annual_value_at_risk_cents // cohort_size, currency
    )
    total = per_customer * treatment_arm(bounds.percentage_max, cohort_size)
    over = bounds.check_cost(total, annual_value_at_risk_cents)
    if over is not None:
        raise OfferRefused(over)
    return per_customer, total
