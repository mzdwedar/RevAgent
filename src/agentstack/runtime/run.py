"""Run identity (Part 4).

Every meaningful execution gets a stable id that ties together the input, the session
state, the tool calls, the waits, the approvals, the retries, the output and the
traces. Without it, a user outcome cannot be walked back to what the system did.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


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
