"""Catch a deploy that would strand live runs, before it ships (criterion 22).

T19 made an incompatible checkpoint park its run instead of resuming it wrongly. That
is the safety net. This is the thing that stops us needing it by accident: a change to
`TurnState` or to the graph's nodes that old checkpoints cannot survive, shipped with
nobody having said so.

`checkpoints/schema.json` records the shape this code writes. The *last release* is
that file as it stands at `--base`: the base branch in CI, `HEAD` locally, a release
tag once there are tags. Against it:

- a compatible change (a key or a node added) passes;
- an incompatible one (a key removed or retyped, a node removed or renamed) fails
  unless it bumps `CHECKPOINT_SCHEMA_VERSION`, stops claiming to resume the old
  version, and ships a migration note at `checkpoints/v{N}.md` - which is what the
  operator reads when `operator stalled` lists the runs it parked.

The recorded file must also match the code, so the next release compares against the
truth. `--write` brings it up to date; it never makes a breaking change pass.

    uv run python scripts/checkpoint_guard.py                 # worktree vs HEAD
    uv run python scripts/checkpoint_guard.py --base main     # branch vs main
    uv run python scripts/checkpoint_guard.py --write         # record the current shape

Exit codes: 0 clean, 1 findings, 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "checkpoints"
RECORD = DIRECTORY / "schema.json"

Shape = dict[str, Any]


@dataclass(frozen=True)
class Finding:
    what: str
    why: str


def breaking_changes(released: Shape, current: Shape) -> list[str]:
    """What an old checkpoint could not survive. Additions are not in this list.

    `TurnState` is `total=False`, so a key an old checkpoint lacks reads as absent,
    which the nodes already handle. A key that vanished or changed type is read as
    something it is not, and a node that vanished is somewhere a pending checkpoint
    cannot resume to.
    """
    found: list[str] = []
    before, after = released["fields"], current["fields"]
    for name, kind in sorted(before.items()):
        if name not in after:
            found.append(f"field {name!r} removed")
        elif after[name] != kind:
            found.append(f"field {name!r} changed type: {kind} -> {after[name]}")
    found.extend(
        f"node {node!r} removed" for node in sorted(set(released["nodes"]) - set(current["nodes"]))
    )
    return found


def judge(
    *,
    released: Shape | None,
    recorded: Shape | None,
    current: Shape,
    compatible: frozenset[int],
    notes: Path = DIRECTORY,
) -> list[Finding]:
    findings: list[Finding] = []
    if recorded != current:
        findings.append(
            Finding(
                "checkpoints/schema.json is not the shape this code writes",
                "the next release would be compared against a shape nobody ships; "
                "run `uv run python scripts/checkpoint_guard.py --write`",
            )
        )
    if released is None:
        return findings

    was, now = released["version"], current["version"]
    if now < was:
        findings.append(
            Finding(
                f"checkpoint schema version went backwards, v{was} -> v{now}",
                f"checkpoints already written as v{was} would be read as an older shape",
            )
        )
        return findings

    breaking = breaking_changes(released, current)
    if not breaking:
        return findings
    detail = "; ".join(breaking)

    if now == was:
        findings.append(
            Finding(
                f"incompatible change to checkpoint schema v{was} without a version bump",
                f"{detail}. Live runs stopped at a v{was} checkpoint would resume into a "
                "shape that no longer means what they wrote. Bump "
                "CHECKPOINT_SCHEMA_VERSION in src/agentstack/runtime/graph.py.",
            )
        )
        return findings

    if was in compatible:
        findings.append(
            Finding(
                f"v{now} still claims to resume v{was}",
                f"{detail}. COMPATIBLE_SCHEMA_VERSIONS is what lets a checkpoint through; "
                f"listing v{was} there resumes those runs into the new shape instead of "
                "parking them.",
            )
        )
    note = notes / f"v{now}.md"
    if not note.is_file() or not note.read_text().strip():
        findings.append(
            Finding(
                f"incompatible checkpoint change ships no migration note at checkpoints/v{now}.md",
                f"{detail}. Runs on v{was} will park in needs_migration, and the note is "
                "what tells the operator how to migrate and release them.",
            )
        )
    return findings


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def released_at(base: str) -> Shape | None:
    """The recorded shape at `base`, or None if that release recorded none."""
    if _git("rev-parse", "--verify", f"{base}^{{commit}}").returncode != 0:
        raise LookupError(f"{base!r} is not a commit this repository knows")
    shown = _git("show", f"{base}:{RECORD.relative_to(ROOT)}")
    return json.loads(shown.stdout) if shown.returncode == 0 else None


def _render(shape: Shape) -> str:
    return json.dumps(shape, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail a checkpoint change that strands runs.")
    parser.add_argument("--base", default="HEAD", help="the last release to compare against")
    parser.add_argument("--write", action="store_true", help="record the current shape")
    args = parser.parse_args(argv)

    from agentstack.runtime import graph

    current = graph.checkpoint_shape()
    if args.write:
        RECORD.parent.mkdir(exist_ok=True)
        RECORD.write_text(_render(current))
        print(f"checkpoint_guard: recorded v{current['version']} in {RECORD.relative_to(ROOT)}")

    try:
        released = released_at(args.base)
    except LookupError as exc:
        print(f"checkpoint_guard: could not run: {exc}", file=sys.stderr)
        return 2
    recorded = json.loads(RECORD.read_text()) if RECORD.is_file() else None

    findings = judge(
        released=released,
        recorded=recorded,
        current=current,
        compatible=graph.COMPATIBLE_SCHEMA_VERSIONS,
    )
    for finding in findings:
        print(f"  {finding.what}\n      {finding.why}")
    if findings:
        print(f"checkpoint_guard: {len(findings)} finding(s) against {args.base}")
        return 1
    against = "nothing released yet" if released is None else f"v{released['version']}"
    print(f"checkpoint_guard: v{current['version']} is safe to ship over {against}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
