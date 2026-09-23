"""A process that starts a turn and hangs inside it, to be killed from outside.

Criterion 1 says a parked run resumes "from a checkpoint rebuilt in a *fresh process*".
A resume in the process that wrote the checkpoint demonstrates almost nothing: the
objects are still in memory, the pool is still open, and a checkpointer that never
wrote a row would pass. So this is a real script, started with `python -m`, killed with
SIGKILL, and never given the chance to clean up after itself.

It is importable as well as runnable, because the test needs the same recording engine
on the other side of the kill to count model calls across both processes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

from agentstack.interfaces.wiring import build_stack, envelope_for
from agentstack.runtime.loop import run_turn
from agentstack.runtime.run import Run
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import open_pool

SCOPES = frozenset({"billing:read", "billing:refund"})
MESSAGE = "lookup_subscription tenant=acme customer_id=c-42"
# A stray worker must not outlive the test that started it.
HANG_LIMIT_SECONDS = 60.0


class RecordingEngine:
    """Appends a line per model call to a file both processes can read.

    A file rather than a counter, because the two counts happen in different processes
    and the whole question is what survived between them.
    """

    def __init__(self, inner: Any, path: Path) -> None:
        self.inner = inner
        self.asset = inner.asset
        self.path = path

    def generate(self, request: Any) -> Any:
        with self.path.open("a") as handle:
            handle.write(f"{os.getpid()}\n")
        return self.inner.generate(request)


class HangingClient:
    """A surface that never answers, so the turn stops inside `act`.

    Announces that it has been reached before hanging. That marker is what tells the
    parent the model call is already checkpointed - killing earlier would prove nothing
    about resuming past it.
    """

    def __init__(self, inner: Any, reached: Path) -> None:
        self.inner = inner
        self.reached = reached

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        # The resource is recorded, not ignored: the parent checks the worker died
        # inside the read it expected rather than somewhere else entirely.
        self.reached.write_text(f"{os.getpid()} {resource} {sorted(query)}\n")
        deadline = time.monotonic() + HANG_LIMIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.05)
        raise TimeoutError("the parent never killed this worker")

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        return str(self.inner.commit(resource, payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--model-calls", required=True, type=Path)
    parser.add_argument("--reached", required=True, type=Path)
    args = parser.parse_args(argv)

    checkpoints, saver = open_checkpointer(args.database_url)
    with open_pool(args.database_url, min_size=1, max_size=2) as pool:
        stack = build_stack(Database(pool=pool), saver)
        from agentstack.tools.spec import Surface

        stack.deps.engine = RecordingEngine(stack.deps.engine, args.model_calls)
        stack.deps.gateway.surfaces[Surface.API] = HangingClient(stack.client, args.reached)

        run = Run(
            run_id=args.run_id,
            session_id=args.session_id,
            tenant="acme",
            user="agent-operator",
            channel="durability",
        )
        view = stack.resolver.resolve(
            session_id=args.session_id, user_id="agent-operator", tenant="acme"
        )
        run_turn(
            run=run,
            envelope=envelope_for(view, scopes=SCOPES),
            message=MESSAGE,
            deps=stack.deps,
            turn_id=args.turn_id,
        )
    checkpoints.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
