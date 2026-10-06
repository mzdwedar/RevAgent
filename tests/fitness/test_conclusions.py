"""Learning loop L5, the policy half (SPEC criterion 7).

The two gates named in the todo, `a-negative-insv-never-promotes` and
`a-promotion-still-requires-approval`, as tests, plus the rule's edges.
"""

from __future__ import annotations

import inspect
import random
from dataclasses import replace
from pathlib import Path

import pytest

from agentstack.context.readout import Estimate, Readout
from agentstack.policy import conclusions
from agentstack.policy.conclusions import ConclusionRule, conclude
from agentstack.tools import experiments
from agentstack.tools.spec import Approval

RULE = ConclusionRule.load()


def _readout(lower: float, point: float, upper: float, *, simulated: bool = True) -> Readout:
    est = Estimate(0.0, 0.0)
    return Readout(
        experiment_version="exp:1", offer="free-month", simulated=simulated, treated=10,
        control=10, adjusted=est, unadjusted=est, variance_ratio=0.8,
        covariate_correlation=0.5, offer_cost_cents=1, insv_cents=point,
        insv_lower_cents=lower, insv_upper_cents=upper, covariate_definition="d",
    )  # fmt: skip


def test_the_rule_is_read_from_config() -> None:
    assert RULE.promote_if_lower_above_cents == 0
    assert RULE.stop_if_upper_below_cents == 0
    assert RULE.max_extensions == 1


def test_promote_stop_extend() -> None:
    assert conclude(_readout(1, 5, 9), extensions_used=0, rule=RULE).action == "promote"
    assert conclude(_readout(-9, -5, -1), extensions_used=0, rule=RULE).action == "stop"
    assert conclude(_readout(-3, 1, 5), extensions_used=0, rule=RULE).action == "extend"


def test_an_interval_that_straddles_extends_once_then_stops() -> None:
    straddle = _readout(-3, 1, 5)
    assert conclude(straddle, extensions_used=0, rule=RULE).action == "extend"
    assert conclude(straddle, extensions_used=1, rule=RULE).action == "stop"


def test_a_lower_bound_of_exactly_zero_does_not_promote() -> None:
    assert conclude(_readout(0, 4, 8), extensions_used=0, rule=RULE).action != "promote"


def test_a_raised_floor_is_honoured() -> None:
    strict = replace(RULE, promote_if_lower_above_cents=100)
    assert conclude(_readout(50, 80, 120), extensions_used=0, rule=strict).action != "promote"


def test_simulated_travels_into_the_conclusion() -> None:
    assert conclude(_readout(1, 5, 9), extensions_used=0, rule=RULE).simulated is True
    assert (
        conclude(_readout(1, 5, 9, simulated=False), extensions_used=0, rule=RULE).simulated
        is False
    )


def test_a_negative_insv_never_promotes() -> None:
    """a-negative-insv-never-promotes: lower <= point, so a point at or below zero cannot
    have a lower bound above zero. Fuzzed over intervals and extension counts."""
    rng = random.Random(5)
    for _ in range(5_000):
        point = -abs(rng.gauss(0, 1e6))
        lower = point - abs(rng.gauss(0, 1e6))
        upper = point + abs(rng.gauss(0, 1e6))
        got = conclude(_readout(lower, point, upper), extensions_used=rng.randint(0, 3), rule=RULE)
        assert got.action != "promote"


def test_a_promotion_still_requires_approval() -> None:
    """a-promotion-still-requires-approval: promote is a decision to propose the rollout,
    and the rollout tool stops at its approval boundary. The policy module can neither
    commit nor execute."""
    assert experiments.ROLLOUT.approval is Approval.ALWAYS
    text = Path(conclusions.__file__).read_text()
    code = text.split('"""', 2)[2]
    for forbidden in ("agentstack.execution", "agentstack.runtime", "agentstack.model", "commit("):
        assert forbidden not in code


def test_the_decision_takes_only_a_readout() -> None:
    assert list(inspect.signature(conclude).parameters) == ["readout", "extensions_used", "rule"]


@pytest.mark.parametrize("action", ["promote", "stop", "extend"])
def test_the_rule_is_recorded_on_the_conclusion(action: str) -> None:
    readouts = {"promote": (1, 5, 9), "stop": (-9, -5, -1), "extend": (-3, 1, 5)}
    got = conclude(_readout(*readouts[action]), extensions_used=0, rule=RULE)
    assert got.rule.record() == {
        "promote_if_lower_above_cents": 0.0,
        "stop_if_upper_below_cents": 0.0,
        "max_extensions": 1,
    }
