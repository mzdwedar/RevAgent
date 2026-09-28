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
from datetime import timedelta

TASK_QUEUE = "experiment-runs"

# Activities are named here and registered by the worker. Workflow code refers to these
# strings, never to `activities.py`: importing it would drag the gateway and the
# driver into the sandbox, and the sandbox would not object (E4).
ENSURE_RUN = "ensure_run"
EVALUATE_CYCLE = "evaluate_cycle"
PARK_TRIGGER_WAIT = "park_trigger_wait"
SATISFY_TRIGGER_WAIT = "satisfy_trigger_wait"
RUN_TURN = "run_turn"
ASK_APPROVAL = "ask_approval"
COMMIT = "commit"

# The stage a turn runs at, which decides the tools it is shown (T22). Named here
# because workflow code may not import `agentstack.tools`, where the stages are defined;
# tests hold these equal to `tools.experiments.DRAFT_STAGE` and `ROLLOUT_STAGE`.
DRAFT = "draft"
ROLLOUT = "rollout"

# How long a question waits unanswered before it is put again. Named here because
# workflow code may not import `runtime.waits`; a test holds it equal to
# `waits.APPROVAL_REASK_AFTER`, which is what the wait row's deadline is set from.
REASK_EVERY = timedelta(hours=24)

# The status of a turn or an act that met an effect of unknown outcome and parked a
# reconcile wait: the run waits for a person to settle the claim, then goes again.
UNRESOLVED = "unresolved"


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
    # Set only when the run continues as new: what the next execution takes over. A field
    # rather than a second argument, so a start with one argument still decodes.
    carried: Carried | None = None


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
class Carried:
    """What a run takes into its next execution when it continues as new (T46).

    Counts, a watermark and trigger ids. Everything else is either in Postgres already
    or empty at the moment a run may continue: no approval or reconcile wait is open, no
    activity is in flight, and no trigger wait is parked.
    """

    # Names the next trigger wait. Reset, the new execution would park `…-trigger-0`
    # again and be handed back the first wait, long since satisfied.
    waits_parked: int
    # What the next trigger wait waits *after*: its snapshot.
    last_watermark: str
    # Triggers handed over and not yet evaluated, in the order they arrived.
    pending: tuple[Trigger, ...] = ()
    # Cycles evaluated by earlier executions, so progress can say what the run did.
    cycles_before: int = 0


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
class TriggerWaitIntent:
    """Park the run's `sequence`-th trigger wait. The activity makes the id from these,
    so a rerun parks the same wait: `sequence` is workflow state, and replay gives it
    back unchanged. It is not a Temporal id (E1)."""

    run_id: str
    sequence: int
    # The watermark the run last evaluated: what it's waiting *after*. The wait's snapshot.
    after_data_as_of: str


@dataclass(frozen=True, slots=True)
class ParkedWait:
    wait_id: str
    # Seconds until the deadline, as the record has it. Zero or less: already overdue.
    due_in_s: float


@dataclass(frozen=True, slots=True)
class TriggerArrived:
    wait_id: str
    trigger: Trigger


@dataclass(frozen=True, slots=True)
class TurnIntent:
    """Run one turn of this run, at this stage, about this cycle.

    Ids only. The instruction the model reads is built inside the activity from the
    cycle's record, and the envelope is minted there too: neither is ever history.
    """

    run_id: str
    stage: str
    experiment_id: str
    data_as_of: str
    kind: str


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """How the turn ended, as counts. What the model said stays in the transcript and
    the trace; receipts and refusals stay in the audit trail."""

    status: str
    receipts: int = 0
    refusals: int = 0
    # Set when the activity itself was refused (a gateway or layer-8 refusal type).
    refusal: str | None = None
    # Set when the turn stopped for a person: the approval wait it parked, or, with
    # status `UNRESOLVED`, the reconcile wait for an effect of unknown outcome.
    wait_id: str | None = None


@dataclass(frozen=True, slots=True)
class AskIntent:
    """Put this wait's question to a person. `asked` counts the times before this one:
    0 is the first ask, and each re-ask after a timer is one more."""

    run_id: str
    wait_id: str
    experiment_id: str
    experiment_version: str
    asked: int


@dataclass(frozen=True, slots=True)
class AskResult:
    """Whether the wait had already been answered when the ask came round.

    The answer normally arrives as a signal, sent by the callback after layer 8
    recorded it. If that signal was lost, the next ask finds the wait satisfied and
    says so here: the timer is the backstop, so a lost wake-up costs one interval,
    not the run.
    """

    answered: bool


@dataclass(frozen=True, slots=True)
class CommitIntent:
    """Commit what this answered wait asked about. The irreversible act.

    Names the wait and the cycle whose rollout turn proposed the action, and nothing
    else. No snapshot: the activity reads the world at the act, so an approval given
    against a world that has since moved is stale (ADR-0008 rule 3). No proposal and no
    envelope either: the proposal is read from the turn's checkpoint, and the envelope is
    minted in the activity.
    """

    run_id: str
    wait_id: str
    experiment_id: str
    data_as_of: str
    kind: str


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    """How the act ended: `committed`, `deduplicated`, `unresolved` or `refused`.

    `unresolved` names the reconcile wait the run parked: the effect may have applied,
    and only the surface knows (E2). `refused` names the refusal type. The receipt stays
    in the ledger and the audit trail.
    """

    status: str
    wait_id: str | None = None
    refusal: str | None = None


@dataclass(frozen=True, slots=True)
class RunProgress:
    """Where the run is, for whoever asks. Read by query, so it's never history."""

    run_id: str
    recorded: bool
    pending_triggers: int
    cycles: tuple[CycleResult, ...]
    # The trigger wait the run is parked on, if any, and whether its deadline has passed.
    # Overdue is position, not record: `operator stalled` reads the `waits` row.
    waiting_on: str | None = None
    overdue: bool = False
    turns: tuple[TurnOutcome, ...] = ()
    # The approval the run is parked on, how many times it has been put, and the waits
    # whose answers have arrived.
    awaiting_approval: str | None = None
    asks: int = 0
    answered: tuple[str, ...] = ()
    # Every attempt at an act, in order, and the reconcile wait the run is parked on.
    commits: tuple[CommitOutcome, ...] = ()
    reconciling: str | None = None
    # Cycles evaluated before the run last continued as new; `cycles` holds the rest.
    cycles_before: int = 0
