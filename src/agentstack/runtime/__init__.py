"""Layer 3 - runtime, workflows, durable execution.

A loop can answer a turn. A durable workflow can survive time. This package owns
progress - run identity, step boundaries, persisted waits, resume - not authority.
The backend behind the reference implementations here is ADR-0002; the invariants are
not negotiable whichever backend wins.
"""

from agentstack.runtime.loop import TurnResult, run_turn
from agentstack.runtime.run import Run, new_run
from agentstack.runtime.snapshot import resource_snapshot
from agentstack.runtime.steps import StepLedger, StepRecord
from agentstack.runtime.waits import ResumeEvent, ResumeRejected, Wait, WaitStore, resume

__all__ = [
    "ResumeEvent",
    "ResumeRejected",
    "Run",
    "StepLedger",
    "StepRecord",
    "TurnResult",
    "Wait",
    "WaitStore",
    "new_run",
    "resource_snapshot",
    "resume",
    "run_turn",
]
