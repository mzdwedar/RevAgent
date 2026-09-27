"""What crosses between workflow and activity: ids and small verdicts, nothing else.

Everything in these dataclasses is written into Temporal's event history, and history is
kept, replayed and shown in a UI. So an envelope, a credential, a prompt, a snapshot or
an evidence bundle never appears here. An activity is handed an id and reads the rest
from Postgres itself, at the moment it acts (rule 3, ADR-0008).

This module is on the workflow's side of contract 6. It imports the standard library
and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass


def workflow_id(run_id: str) -> str:
    """The one workflow per run. The server refuses a second start under the same id
    (P1), which is what makes five concurrent starts one run."""
    return f"experiment-run:{run_id}"


@dataclass(frozen=True, slots=True)
class RunStart:
    run_id: str


@dataclass(frozen=True, slots=True)
class RunEnd:
    run_id: str
    status: str
