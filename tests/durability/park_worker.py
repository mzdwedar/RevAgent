"""Park a run on a human approval, announce it, and wait to be killed.

Separate from `worker.py` because it stops somewhere else: that one hangs inside a
surface read, this one parks on an approval and then sits doing nothing, which is what
a real run waiting for a person looks like.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from agentstack.interfaces.wiring import build_stack
from agentstack.policy.prompt import ApprovalPrompt
from agentstack.runtime.run import new_run
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import open_pool
from agentstack.tools.experiments import ROLLOUT, ROLLOUT_STAGE, prepare_rollout
from agentstack.tools.spec import ActsAs

TENANT = "acme"
USER = "agent-operator"
SCOPES = frozenset({"experiments:draft", "experiments:rollout"})
ARGS = {
    "tenant": TENANT,
    "experiment_id": "exp-7",
    "experiment_version": "exp:5cbf2762",
    "percentage": 10,
    "targeting_model_version": "tabpfn-3.5",
    "risk_threshold": 0.61,
    "prior_rollout_event": 0,
}
WAIT_LIMIT_SECONDS = 60.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--parked", required=True, type=Path)
    args = parser.parse_args(argv)

    checkpoints, saver = open_checkpointer(args.database_url)
    with open_pool(args.database_url, min_size=1, max_size=2) as pool:
        stack = build_stack(Database(pool=pool), saver)
        run = stack.runs.ensure(
            new_run(
                session_id=args.session_id,
                tenant=TENANT,
                user=USER,
                stage=ROLLOUT_STAGE,
                channel="durability",
            )
        )
        request = prepare_rollout(ARGS)
        summary = ApprovalPrompt(
            spec=ROLLOUT,
            resource=request.resource,
            payload=request.payload,
            principal=USER,
            acts_as=ActsAs.DELEGATED,
            requested_by=USER,
            channel="durability",
        ).render()
        wait = stack.waits.park(
            run_id=run.run_id,
            kind="human_approval",
            state_snapshot="as-shown",
            action_fingerprint=request.fingerprint(),
            approval_summary=summary,
        )
        args.parked.write_text(f"{run.run_id} {wait.wait_id}\n")

        # A run waiting for a person does nothing. The parent kills this.
        deadline = time.monotonic() + WAIT_LIMIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.05)
    checkpoints.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
