"""How long a run waits for its next trigger before it is stalled (SPEC.md criterion 4).

Read from `experiments/cadence.toml`, like the targeting rule, because it's a claim about
the data source and not about this code. `WaitStore.park` refuses a trigger wait
without a deadline, and this is where the deadline comes from.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[3] / "experiments" / "cadence.toml"

_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class CadenceNotUnderstood(ValueError):
    """The cadence file names a profile or a duration this code can't read."""


def duration(text: str) -> timedelta:
    """`90s`, `15m`, `36h`, `7d`. A bare number is refused: hours or days is not a guess."""
    match = re.fullmatch(r"(\d+)([smhd])", text.strip())
    if match is None:
        raise CadenceNotUnderstood(f"{text!r} is not a duration like 30m, 36h or 7d")
    amount, unit = match.groups()
    return timedelta(**{_UNITS[unit]: int(amount)})


@dataclass(frozen=True, slots=True)
class TriggerCadence:
    profile: str
    trigger_deadline: timedelta

    @staticmethod
    def load(profile: str = "default", path: Path = DEFAULTS) -> TriggerCadence:
        profiles = tomllib.loads(path.read_text())["profiles"]
        if profile not in profiles:
            raise CadenceNotUnderstood(
                f"{profile!r} is not a cadence profile; known: {sorted(profiles)}"
            )
        deadline = duration(str(profiles[profile]["trigger_deadline"]))
        if deadline <= timedelta(0):
            raise CadenceNotUnderstood(f"a trigger cannot be due {deadline} after the last one")
        return TriggerCadence(profile=profile, trigger_deadline=deadline)
