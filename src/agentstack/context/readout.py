"""The readout (layer 5): did the offer save more than it cost?

One terminal regression at the pre-declared endpoint, `revenue ~ treatment + eLTV_pre`,
with HC2 standard errors. Pure: no I/O, no clock, the same inputs give the same numbers.

INSV = (saved LTV_t - saved LTV_c) - offer cost, where saved LTV is observed revenue per
subscriber in the outcome window, so the difference is tau-hat times the treated headcount
and the cost is the recorded per-treated cost times the same headcount. The covariate only
sharpens tau-hat. It is not an outcome and no predicted quantity is summed into INSV
(SPEC criterion 12). `simulated` is carried from the outcomes and has no default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from agentstack.context.assignment import Assignment
from agentstack.context.outcomes import Outcomes
from agentstack.prediction.covariate import Covariate

Z95 = 1.959964


class ReadoutRefused(ValueError):
    """The data cannot support an estimate."""


@dataclass(frozen=True, slots=True)
class Estimate:
    tau_cents: float
    se_cents: float

    @property
    def lower(self) -> float:
        return self.tau_cents - Z95 * self.se_cents

    @property
    def upper(self) -> float:
        return self.tau_cents + Z95 * self.se_cents


@dataclass(frozen=True, slots=True)
class Readout:
    experiment_version: str
    offer: str
    simulated: bool
    treated: int
    control: int
    adjusted: Estimate
    unadjusted: Estimate
    variance_ratio: float  # adjusted variance / unadjusted variance; below 1 is a gain
    covariate_correlation: float
    offer_cost_cents: int
    insv_cents: float
    insv_lower_cents: float
    insv_upper_cents: float
    covariate_definition: str


def _fit(y: np.ndarray, columns: list[np.ndarray]) -> Estimate:
    """OLS with an intercept; the treatment is the first column given. HC2 errors."""
    x = np.column_stack([np.ones(len(y)), *columns])
    xtx_inv = np.linalg.inv(x.T @ x)
    beta = xtx_inv @ x.T @ y
    resid = y - x @ beta
    leverage = np.einsum("ij,jk,ik->i", x, xtx_inv, x)
    meat = (x * (resid**2 / (1.0 - leverage))[:, None]).T @ x
    cov = xtx_inv @ meat @ xtx_inv
    return Estimate(tau_cents=float(beta[1]), se_cents=math.sqrt(float(cov[1, 1])))


def readout(outcomes: Outcomes, assignment: Assignment, covariate: Covariate) -> Readout:
    """The estimate for `outcomes` under `assignment`, adjusted for `covariate`."""
    subscribers = [row.subscriber for row in outcomes.rows]
    by_subscriber = assignment.arms()
    if any(s not in by_subscriber for s in subscribers):
        raise ReadoutRefused("an outcome belongs to a subscriber with no arm")
    arms = [by_subscriber[s] for s in subscribers]
    treat = np.array([a == "treatment" for a in arms], dtype=float)
    treated, control = int(treat.sum()), int(len(treat) - treat.sum())
    if treated < 2 or control < 2:
        raise ReadoutRefused(f"{treated} treated and {control} control; need 2 of each")

    y = np.array([row.revenue_cents for row in outcomes.rows], dtype=float)
    z = np.array([covariate.values[s] for s in subscribers], dtype=float)
    if z.std() == 0.0:
        raise ReadoutRefused("the covariate does not vary, so it cannot adjust anything")

    adjusted = _fit(y, [treat, z])
    unadjusted = _fit(y, [treat])
    cost = outcomes.offer_cost_cents

    def insv(tau: float) -> float:
        return (tau - cost) * treated

    return Readout(
        experiment_version=outcomes.experiment_version,
        offer=outcomes.offer,
        simulated=outcomes.simulated,
        treated=treated,
        control=control,
        adjusted=adjusted,
        unadjusted=unadjusted,
        variance_ratio=(adjusted.se_cents / unadjusted.se_cents) ** 2,
        covariate_correlation=float(np.corrcoef(y, z)[0, 1]),
        offer_cost_cents=cost,
        insv_cents=insv(adjusted.tau_cents),
        insv_lower_cents=insv(adjusted.lower),
        insv_upper_cents=insv(adjusted.upper),
        covariate_definition=covariate.definition,
    )
