"""Catch a workflow change that live runs could not survive, before it ships (criterion 41).

A Temporal run is its history. On every recovery the worker replays that history through
the code it is running *now*, and a change that makes the code ask for something the
history doesn't hold (an activity inserted, a timer moved, a step reordered) fails the
run with a nondeterminism error, weeks into its life. `checkpoint_guard.py` asks the
same question of the turn graph's checkpoints; this asks it of `ExperimentWorkflow`.

The histories are `tests/fixtures/histories/*.json`, recorded from real runs of the
production worker by `tests/durability/test_recorded_histories.py`:

- `trigger_cycles`: a cycle that abstains, a trigger wait that goes overdue on its
  timer, a `metric_movement` refused by layer 8, the run parked again;
- `approved_commit`: propose, draft, rollout turn, park, ask, a day unanswered, ask
  again, approved in Slack, committed;
- `refused_at_the_act`: woken with no approval on record, refused by the gateway;
- `unresolved_reconciled`: the rollout's answer lost, a reconcile wait, woken,
  deduplicated.

A change that must reach live runs goes behind `workflow.patched("<id>")`, which
replays the old history down the old branch. The guard then passes.

Recording is deliberate. `--record` reruns those scenarios with
`AGENTSTACK_RECORD_HISTORIES=1` and rewrites the files, then replays them. Re-record
when a scenario's path changes on purpose, and commit the diff. Until a run is live,
replacing a history is fine. After that, a history is what some live run looks like:
add the new one beside it rather than overwrite it, and delete one only once no run
of that shape can still be open (ask first; SPEC-durable-runtime.md, Boundaries).

    uv run python scripts/replay_guard.py              # replay every recorded history
    uv run python scripts/replay_guard.py --record     # re-record them, then replay

Recording needs the substrate (`DATABASE_URL`); replaying needs nothing but this code.

Exit codes: 0 clean, 1 findings, 2 could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

ROOT = Path(__file__).resolve().parents[1]
HISTORIES = ROOT / "tests" / "fixtures" / "histories"
RECORDER = ROOT / "tests" / "durability" / "test_recorded_histories.py"
# Read by `tests/temporal_support.keep_history`; a fitness test holds the two equal.
RECORD_HISTORIES = "AGENTSTACK_RECORD_HISTORIES"


@dataclass(frozen=True)
class Finding:
    what: str
    why: str


def load(directory: Path = HISTORIES) -> dict[str, WorkflowHistory]:
    """Every recorded history, by file name. The name doubles as the workflow id,
    which the files don't carry and replay doesn't read."""
    return {
        path.stem: WorkflowHistory.from_json(path.stem, path.read_text())
        for path in sorted(directory.glob("*.json"))
    }


async def replay(
    histories: dict[str, WorkflowHistory], workflows: Sequence[type] | None = None
) -> list[Finding]:
    """Replay each history through `workflows` (this code's, by default) and say which
    ones it could not reproduce."""
    if workflows is None:
        from agentstack.runtime.temporal.workflows import ExperimentWorkflow

        workflows = [ExperimentWorkflow]

    async def each() -> AsyncIterator[WorkflowHistory]:
        for history in histories.values():
            yield history

    results = await Replayer(workflows=workflows).replay_workflows(
        each(), raise_on_replay_failure=False
    )
    by_run = {history.run_id: name for name, history in histories.items()}
    return [
        Finding(
            f"{by_run.get(run_id, run_id)}.json no longer replays",
            f"{type(failure).__name__}: {_reason(failure)}. A live run with this history "
            "would fail on its next recovery. Put the change behind workflow.patched(), or, "
            "if the path changed on purpose and nothing is live, re-record with --record.",
        )
        for run_id, failure in sorted(results.replay_failures.items(), key=lambda f: f[0])
    ]


def _reason(failure: Exception) -> str:
    """The SDK's own sentence (`[TMPRL1100] ... HistoryEvent(id: 15, ...)`), without the
    activation dump around it."""
    found = re.search(r'message: "([^"]*)"', str(failure))
    return found.group(1) if found else str(failure).splitlines()[0]


def record() -> int:
    """Rerun the recording scenarios with recording switched on."""
    env = os.environ | {RECORD_HISTORIES: "1"}
    command = ["uv", "run", "pytest", "-q", str(RECORDER)]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail a workflow change that strands runs.")
    parser.add_argument("--record", action="store_true", help="re-record the histories first")
    args = parser.parse_args(argv)

    if args.record and record() != 0:
        print("replay_guard: could not run: recording failed", file=sys.stderr)
        return 2

    histories = load()
    if not histories:
        # A guard with nothing to replay passes everything, which is not a guard.
        print(
            f"replay_guard: could not run: no histories in {HISTORIES.relative_to(ROOT)}; "
            "record them with `uv run python scripts/replay_guard.py --record`",
            file=sys.stderr,
        )
        return 2

    findings = asyncio.run(replay(histories))
    for finding in findings:
        print(f"  {finding.what}\n      {finding.why}")
    if findings:
        print(f"replay_guard: {len(findings)} of {len(histories)} histories no longer replay")
        return 1
    print(f"replay_guard: {len(histories)} recorded histories replay against this code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
