"""Run identity (Part 4).

Every meaningful execution gets a stable id that ties together the input, the session
state, the tool calls, the waits, the approvals, the retries, the output and the
traces. Without it, a user outcome cannot be walked back to what the system did.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from agentstack.storage.database import Database
from agentstack.tools.experiments import EVALUATION_STAGE, experiment_resource

# Stages whose runs are about exactly one experiment and must say which. Evaluation is
# woken by a trigger that names one, and holds `experiments:halt`: unbound, that scope
# reaches every experiment in the tenant (H3). A rollout run *may* be bound, and is
# then held to it; it is not required to be, because its one irreversible act already
# waits on a human approval bound to the action's fingerprint - which names the
# experiment - and rollout runs parked before `runs.subject` existed must still resume.
# A draft run is never bound: it names an experiment nobody has written yet.
SUBJECT_REQUIRED = frozenset({EVALUATION_STAGE})


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    session_id: str
    tenant: str
    user: str
    stage: str = "default"
    # Where the request came from. A string, not an import: the runtime records
    # provenance without depending on the channel layer.
    channel: str = "unknown"
    # The experiment this run is about, from the trigger that woke it or from whoever
    # created the run - never from model or tool output. None: bound to nothing.
    subject: str | None = None

    def __post_init__(self) -> None:
        if self.subject is not None and (not self.subject.strip() or "/" in self.subject):
            raise ValueError(f"a run's subject is one experiment id; got {self.subject!r}")
        if self.subject is None and self.stage in SUBJECT_REQUIRED:
            # Refused rather than defaulted to unbound: an evaluation run that cannot
            # say which experiment it is about holds halt authority over all of them.
            # This is also what a pre-0013 row nothing could backfill hits on load.
            raise ValueError(
                f"run {self.run_id} is on the {self.stage} stage and names no subject; "
                "an evaluation is about one experiment, and the run must say which"
            )

    def subject_resource(self) -> str | None:
        """The resource root the run's envelope is narrowed to (layer 8 reads it)."""
        return None if self.subject is None else experiment_resource(self.tenant, self.subject)


def new_run_id() -> str:
    return f"run-{uuid.uuid4()}"


def new_run(
    *,
    session_id: str,
    tenant: str,
    user: str,
    stage: str = "default",
    channel: str = "unknown",
    subject: str | None = None,
) -> Run:
    return Run(
        run_id=new_run_id(),
        session_id=session_id,
        tenant=tenant,
        user=user,
        stage=stage,
        channel=channel,
        subject=subject,
    )


@dataclass(frozen=True, slots=True)
class RunStore:
    """Run identity, durably.

    `ensure` rather than `put`: a run is written once and then continued many times,
    and every turn after the first arrives with a run that already exists. Making the
    caller remember which turn it is on would be a worse API than an upsert.
    """

    db: Database

    def ensure(self, run: Run) -> Run:
        self.db.execute(
            "INSERT INTO runs (run_id, session_id, tenant, acting_user, stage, channel, subject)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
            (
                run.run_id,
                run.session_id,
                run.tenant,
                run.user,
                run.stage,
                run.channel,
                run.subject,
            ),
        )
        return run

    def get(self, run_id: str) -> Run | None:
        row = self.db.fetch_one(
            "SELECT run_id, session_id, tenant, acting_user, stage, channel, subject"
            " FROM runs WHERE run_id = %s",
            (run_id,),
        )
        return None if row is None else Run(*row)


@dataclass(frozen=True, slots=True)
class ExperimentRun:
    tenant: str
    experiment_id: str
    run_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class ExperimentRunStore:
    """Which run is an experiment's (SPEC.md: one durable run per experiment).

    `claim` is one statement, like the trigger cycle's: two first deliveries arriving
    together both read "nothing here", and only the insert can decide between them.
    The loser's ids were never written anywhere, so losing leaves nothing behind.
    """

    db: Database

    def claim(self, candidate: ExperimentRun) -> ExperimentRun:
        """Record `candidate` as this experiment's run, or return the one already recorded."""
        row = self.db.fetch_one(
            "INSERT INTO experiment_runs (tenant, experiment_id, run_id, session_id)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (tenant, experiment_id) DO UPDATE"
            "   SET tenant = EXCLUDED.tenant"
            " RETURNING tenant, experiment_id, run_id, session_id",
            (candidate.tenant, candidate.experiment_id, candidate.run_id, candidate.session_id),
        )
        assert row is not None  # the upsert always returns exactly one row
        return ExperimentRun(*row)
