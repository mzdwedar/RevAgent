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

TASK_QUEUE = "experiment-runs"

# Activities are named here and registered by the worker. Workflow code refers to these
# strings, never to `activities.py`: importing it would drag the gateway and the
# driver into the sandbox, and the sandbox would not object (E4).
ENSURE_RUN = "ensure_run"


def workflow_id(run_id: str) -> str:
    """The one workflow per run. The server refuses a second start under the same id
    (P1), which is what makes five concurrent starts one run."""
    return f"experiment-run:{run_id}"


@dataclass(frozen=True, slots=True)
class RunStart:
    """Who the run is, as identifiers. Identity, not authority: an envelope is minted
    inside the activity that acts, from the session, never carried from here."""

    run_id: str
    session_id: str
    tenant: str
    user: str
    stage: str = "default"
    channel: str = "unknown"


@dataclass(frozen=True, slots=True)
class RunEnd:
    run_id: str
    status: str
