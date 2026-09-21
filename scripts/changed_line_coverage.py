"""Coverage of the lines this change touched.

Project coverage is a number you inherited. Coverage of changed lines is a number the
current change can actually move, which is the only kind worth gating on.

Reads the lcov the test run already wrote - running the suite a second time to get a
number is the fastest way to make people stop running it.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LCOV = ROOT / "coverage.lcov"


def _relative(path: str) -> str:
    """lcov writes absolute paths; the diff speaks in repo-relative ones."""
    with contextlib.suppress(ValueError):
        return str(Path(path).resolve().relative_to(ROOT))
    return path


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout


def changed_lines(base: str | None) -> dict[str, set[int]]:
    diff = (
        _git("diff", f"{base}...", "--unified=0") if base else _git("diff", "HEAD", "--unified=0")
    )
    per_file: dict[str, set[int]] = defaultdict(set)
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@") and path.endswith(".py"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or 1)
                per_file[path].update(range(start, start + count))
    return per_file


def covered_lines() -> dict[str, set[int]]:
    if not LCOV.exists():
        return {}
    per_file: dict[str, set[int]] = defaultdict(set)
    path = ""
    for line in LCOV.read_text().splitlines():
        if line.startswith("SF:"):
            path = _relative(line[3:])
        elif line.startswith("DA:"):
            number, hits = line[3:].split(",")[:2]
            if int(hits) > 0:
                per_file[path].add(int(number))
    return per_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="git ref to compare against")
    parser.add_argument("--min", type=float, default=80.0, help="minimum percent")
    args = parser.parse_args()

    if not LCOV.exists():
        print("changed-line coverage: no coverage.lcov; run the suite with --cov first")
        return 0

    changed = changed_lines(args.base)
    covered = covered_lines()

    total = hit = 0
    gaps: list[str] = []
    for path, lines in changed.items():
        if not path.startswith("src/"):
            continue
        measured = covered.get(path, set())
        # Only lines the coverage report knows about are executable lines.
        executable = {n for n in lines if n in measured} | {
            n for n in lines if n in _executable(path)
        }
        if not executable:
            continue
        total += len(executable)
        hit += len(executable & measured)
        missed = sorted(executable - measured)
        if missed:
            gaps.append(f"  {path}: {missed[:12]}{' ...' if len(missed) > 12 else ''}")

    if total == 0:
        print("changed-line coverage: no changed executable lines in src/")
        return 0

    percent = 100.0 * hit / total
    print(f"changed-line coverage: {percent:.1f}% ({hit}/{total}), floor {args.min:.0f}%")
    if gaps:
        print("uncovered:")
        print("\n".join(gaps))
    if percent < args.min:
        print(
            f"FAIL  below the floor in CONSTRAINTS.md. Write the test rather than "
            f"lowering {args.min:.0f}."
        )
        return 1
    return 0


_EXECUTABLE_CACHE: dict[str, set[int]] = {}


def _executable(path: str) -> set[int]:
    """Lines the lcov report lists for this file at all, covered or not."""
    if path in _EXECUTABLE_CACHE:
        return _EXECUTABLE_CACHE[path]
    lines: set[int] = set()
    if LCOV.exists():
        current = ""
        for line in LCOV.read_text().splitlines():
            if line.startswith("SF:"):
                current = _relative(line[3:])
            elif line.startswith("DA:") and current == path:
                lines.add(int(line[3:].split(",")[0]))
    _EXECUTABLE_CACHE[path] = lines
    return lines


if __name__ == "__main__":
    sys.exit(main())
