"""Run identity (Part 4).

Every meaningful execution gets a stable id that ties together the input, the session
state, the tool calls, the waits, the approvals, the retries, the output and the
traces. Without it, a user outcome cannot be walked back to what the system did.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from agentstack.storage.database import Database


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


def new_run(
    *,
    session_id: str,
    tenant: str,
    user: str,
    stage: str = "default",
    channel: str = "unknown",
) -> Run:
    return Run(
        run_id=f"run-{uuid.uuid4()}",
        session_id=session_id,
        tenant=tenant,
        user=user,
        stage=stage,
        channel=channel,
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
            "INSERT INTO runs (run_id, session_id, tenant, acting_user, stage, channel)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
            (run.run_id, run.session_id, run.tenant, run.user, run.stage, run.channel),
        )
        return run

    def get(self, run_id: str) -> Run | None:
        row = self.db.fetch_one(
            "SELECT run_id, session_id, tenant, acting_user, stage, channel"
            " FROM runs WHERE run_id = %s",
            (run_id,),
        )
        return None if row is None else Run(*row)
