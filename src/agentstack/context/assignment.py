"""Who is treated and who is control, inside the frozen cohort (learning loop, L1).

Randomisation happens within the targeted population and nowhere else. A control arm drawn
from the general base would regress to the mean differently from a cohort picked for its risk
score, and the experiment would report a saving that was never there (SPEC.md, "Regression to
the mean is the trap"; criterion 11).

The arm is a hash of the experiment version and the subscriber, so it is the same in every
process and on every replay, and needs no stored list. A subscriber outside the cohort has no
arm at all: there is no third value to mistake for control.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from agentstack.context.targeting import Cohort

Arm = Literal["treatment", "control"]

# Basis points, so a 7% rollout is 700 of 10_000 and no float decides a boundary.
_BUCKETS = 10_000


@dataclass(frozen=True, slots=True)
class Assignment:
    """The split of one frozen cohort at one rollout percentage."""

    cohort: Cohort
    percentage: int

    def __post_init__(self) -> None:
        if not 0 <= self.percentage <= 100:
            raise ValueError(f"percentage must be 0-100, got {self.percentage}")

    def arm(self, subscriber: int) -> Arm | None:
        """`None` for anyone not in the cohort. The percentage never widens the cohort."""
        return self._arm_of_member(subscriber) if self.cohort.includes(subscriber) else None

    def _arm_of_member(self, subscriber: int) -> Arm:
        bucket = _bucket(self.cohort.experiment_version, subscriber)
        return "treatment" if bucket < self._cut else "control"

    def arms(self) -> dict[int, Arm]:
        """Every member's arm: the whole cohort, nobody else."""
        return {m: self._arm_of_member(m) for m in self.cohort.members}

    @property
    def _cut(self) -> int:
        return self.percentage * _BUCKETS // 100


def _bucket(experiment_version: str, subscriber: int) -> int:
    digest = hashlib.sha256(f"{experiment_version}\x00{subscriber}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % _BUCKETS
