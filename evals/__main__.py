"""`uv run python -m evals run --gates`."""

from __future__ import annotations

import argparse
import sys

from evals.runner import load_cases, run_case


def main() -> int:
    parser = argparse.ArgumentParser(prog="evals")
    parser.add_argument("command", choices=["run", "list"])
    parser.add_argument("--gates", action="store_true", help="only the release gates")
    args = parser.parse_args()

    cases = [c for c in load_cases() if c.gate or not args.gates]
    if not cases:
        print("no eval cases found")
        return 1

    if args.command == "list":
        for case in cases:
            print(f"  P{case.part}  {'GATE' if case.gate else '    '}  {case.id}")
            print(f"          {case.description}")
        return 0

    failed = 0
    for case in cases:
        outcome = run_case(case)
        mark = "pass" if outcome.passed else "FAIL"
        print(f"{mark}  P{case.part}  {case.id}  ({outcome.seconds * 1000:.0f}ms)")
        for failure in outcome.failures:
            print(f"        {failure}")
        failed += 0 if outcome.passed else 1

    print(f"\n{len(cases) - failed}/{len(cases)} gate cases pass")
    if failed:
        print("A release gate is what stops a worse version shipping. Fix the code.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
