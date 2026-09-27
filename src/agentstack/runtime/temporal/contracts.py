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
EVALUATE_CYCLE = "evaluate_cycle"


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


@dataclass(frozen=True, slots=True)
class Trigger:
    """A parsed trigger, as strings.

    `policy.triggers.TriggerEvent` is the real type, but contract 6 keeps workflow code
    away from `policy`, so this carries the same fields and the activity rebuilds the
    event. The kind is a string here: authority is decided in the activity, by
    `cycles.evaluate`, never by the workflow reading it.
    """

    kind: str
    experiment_id: str
    data_as_of: str
    tenant: str
    source: str = "unknown"


@dataclass(frozen=True, slots=True)
class CycleResult:
    """What one trigger came to. A verdict, not evidence: the cohort and its reason
    stay in Postgres and the trace.

    `outcome` is None when the cycle was refused, and `refusal` then names the type.
    """

    experiment_id: str
    data_as_of: str
    kind: str
    outcome: str | None
    experiment_version: str | None = None
    refusal: str | None = None


@dataclass(frozen=True, slots=True)
class RunProgress:
    """Where the run is, for whoever asks. Read by query, so it's never history."""

    run_id: str
    recorded: bool
    pending_triggers: int
    cycles: tuple[CycleResult, ...]
