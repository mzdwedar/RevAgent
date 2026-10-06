"""Readout -> promote / stop / extend (layer 8).

A rule, not a judgement. `conclude` takes the numbers in a `Readout` and nothing else: no
model text, no explanation, no hypothesis. The model writes the Slack message after this
has decided, and its words reach no decision here (SPEC-learning-loop boundaries).

Promote only ever means "propose the rollout again". The rollout tool still stops at its
approval boundary, so a promotion needs a person's yes bound to its payload. Stop means a
`halt_rollout`, which sets exposure to zero and nothing else. This module imports neither.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentstack.context.readout import Readout

DEFAULTS = Path(__file__).resolve().parents[3] / "experiments" / "conclusions.toml"

Action = Literal["promote", "stop", "extend"]


@dataclass(frozen=True, slots=True)
class ConclusionRule:
    promote_if_lower_above_cents: float
    stop_if_upper_below_cents: float
    max_extensions: int

    @staticmethod
    def load(path: Path = DEFAULTS) -> ConclusionRule:
        raw = tomllib.loads(path.read_text())["rule"]
        return ConclusionRule(
            promote_if_lower_above_cents=float(raw["promote_if_lower_above_cents"]),
            stop_if_upper_below_cents=float(raw["stop_if_upper_below_cents"]),
            max_extensions=int(raw["max_extensions"]),
        )

    def record(self) -> dict[str, float | int]:
        return {
            "promote_if_lower_above_cents": self.promote_if_lower_above_cents,
            "stop_if_upper_below_cents": self.stop_if_upper_below_cents,
            "max_extensions": self.max_extensions,
        }


@dataclass(frozen=True, slots=True)
class Conclusion:
    action: Action
    reason: str
    experiment_version: str
    offer: str
    simulated: bool  # no default: a conclusion that forgot to say so reads as a result
    rule: ConclusionRule
    extensions_used: int


def conclude(readout: Readout, *, extensions_used: int, rule: ConclusionRule) -> Conclusion:
    """Decide from the INSV interval. Deterministic and total."""
    lower, upper = readout.insv_lower_cents, readout.insv_upper_cents
    if lower > rule.promote_if_lower_above_cents:
        action: Action = "promote"
        reason = f"INSV lower bound {lower:.0f} is above {rule.promote_if_lower_above_cents:.0f}"
    elif upper < rule.stop_if_upper_below_cents:
        action = "stop"
        reason = f"INSV upper bound {upper:.0f} is below {rule.stop_if_upper_below_cents:.0f}"
    elif extensions_used < rule.max_extensions:
        action = "extend"
        reason = f"INSV interval [{lower:.0f}, {upper:.0f}] straddles the bounds"
    else:
        action = "stop"
        reason = (
            f"INSV interval [{lower:.0f}, {upper:.0f}] still straddles the bounds after "
            f"{extensions_used} extension(s)"
        )
    return Conclusion(
        action=action,
        reason=reason,
        experiment_version=readout.experiment_version,
        offer=readout.offer,
        simulated=readout.simulated,
        rule=rule,
        extensions_used=extensions_used,
    )
