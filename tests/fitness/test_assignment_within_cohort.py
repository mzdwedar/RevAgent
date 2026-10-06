"""Learning loop L1 (SPEC criterion 11): control is drawn from the frozen cohort, only.

The arm is a hash of the experiment version and the subscriber. These tests hold the four
properties that make that safe: control is a subset of the cohort, assignment is stable
across processes, the split lands near the rollout percentage, and nobody outside the cohort
gets an arm.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

import pytest

from agentstack.context.assignment import Assignment
from agentstack.context.targeting import Cohort, TargetingRule

N = 1_000
RULE = TargetingRule(
    profile="test",
    risk_quantile=0.9,
    minimum_cohort=10,
    minimum_annual_value_at_risk_cents=1,
    floors_cents=(),
)


def _cohort(members: tuple[int, ...], version: str = "exp:0123456789abcdef") -> Cohort:
    return Cohort(
        experiment_version=version,
        dataset="test",
        data_as_of="2017-02-28",
        model_version="m1",
        rule=RULE,
        risk_threshold=0.5,
        members=members,
        annual_value_at_risk_cents=1,
        risk_quantiles=(0.5,) * 5,
        revenue_note="",
    )


# Members are row indices into a base far larger than the cohort, and not contiguous.
MEMBERS = tuple(range(7, 7 + 3 * N, 3))


def test_control_is_a_subset_of_the_cohort() -> None:
    arms = Assignment(_cohort(MEMBERS), 10).arms()
    control = {s for s, arm in arms.items() if arm == "control"}
    assert control <= set(MEMBERS)
    assert set(arms) == set(MEMBERS)  # everyone in, nobody else


@pytest.mark.parametrize("percentage", [5, 10, 15, 50])
def test_the_split_is_within_two_points_of_the_target(percentage: int) -> None:
    arms = Assignment(_cohort(MEMBERS), percentage).arms()
    share = sum(1 for arm in arms.values() if arm == "treatment") / len(arms)
    assert abs(share - percentage / 100) <= 0.02


def test_zero_treats_nobody_and_a_hundred_treats_everybody() -> None:
    cohort = _cohort(MEMBERS)
    assert set(Assignment(cohort, 0).arms().values()) == {"control"}
    assert set(Assignment(cohort, 100).arms().values()) == {"treatment"}


def test_a_subscriber_outside_the_cohort_has_no_arm() -> None:
    assignment = Assignment(_cohort(MEMBERS), 10)
    outside = [0, 1, 8, 10**6]
    assert all(s not in MEMBERS for s in outside)
    assert [assignment.arm(s) for s in outside] == [None] * len(outside)


def test_assignment_is_stable_across_processes() -> None:
    here = Assignment(_cohort(MEMBERS), 10).arms()
    code = (
        "from tests.fitness.test_assignment_within_cohort import MEMBERS, _cohort\n"
        "from agentstack.context.assignment import Assignment\n"
        "arms = Assignment(_cohort(MEMBERS), 10).arms()\n"
        "print(''.join('t' if arms[m] == 'treatment' else 'c' for m in MEMBERS))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "".join("t" if here[m] == "treatment" else "c" for m in MEMBERS)


def test_a_different_experiment_version_reshuffles_the_split() -> None:
    a = Assignment(_cohort(MEMBERS, "exp:aaaaaaaaaaaaaaaa"), 10).arms()
    b = Assignment(_cohort(MEMBERS, "exp:bbbbbbbbbbbbbbbb"), 10).arms()
    assert a != b


def test_raising_the_percentage_only_adds_treated_subscribers() -> None:
    """Nobody treated at 10% becomes control at 15%: a rollout widens, it never reshuffles."""
    cohort = _cohort(MEMBERS)
    ten = {s for s, arm in Assignment(cohort, 10).arms().items() if arm == "treatment"}
    fifteen = {s for s, arm in Assignment(cohort, 15).arms().items() if arm == "treatment"}
    assert ten < fifteen


def test_a_percentage_outside_zero_to_a_hundred_is_refused() -> None:
    for bad in (-1, 101):
        with pytest.raises(ValueError):
            Assignment(_cohort(MEMBERS), bad)


def test_the_cohort_is_not_widened_by_replacing_it() -> None:
    """`replace` keeps the frozen membership: the assignment reads it, never rebuilds it."""
    cohort = _cohort(MEMBERS)
    narrower = replace(cohort, members=MEMBERS[:10])
    assert Assignment(narrower, 100).arm(MEMBERS[500]) is None
