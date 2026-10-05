"""`uv run python -m evals run --gates`."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentstack.model.engine import EchoEngine, ModelEngine
from agentstack.storage import migrate
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import DEV_DATABASE_URL, open_pool
from agentstack.storage.provision import drop_database, rebuild_database, run_scoped
from evals.behaviour import load_behaviour_cases, run_behaviour_case
from evals.runner import load_cases, run_case


def main() -> int:
    parser = argparse.ArgumentParser(prog="evals")
    parser.add_argument("command", choices=["run", "list", "behave"])
    parser.add_argument("--gates", action="store_true", help="only the release gates")
    parser.add_argument("--engine", default="echo", help="behave: the engine to measure")
    args = parser.parse_args()

    if args.command == "behave":
        return _behave(args.engine)

    cases = [c for c in load_cases() if c.gate or not args.gates]
    if not cases:
        print("no eval cases found")
        return 1

    if args.command == "list":
        for case in cases:
            print(f"  P{case.part}  {'GATE' if case.gate else '    '}  {case.id}")
            print(f"          {case.description}")
        return 0

    failed = _on_a_substrate(lambda db, saver: _run(cases, db, saver))

    print(f"\n{len(cases) - failed}/{len(cases)} gate cases pass")
    if failed:
        print("A release gate is what stops a worse version shipping. Fix the code.")
    return 1 if failed else 0


def _on_a_substrate(work: Callable[[Database, Any], int]) -> int:
    # The gates run against a real substrate of their own. Sharing the dev database
    # would leave eval sessions in it, and sharing the test database would make the
    # gate result depend on whether pytest had run first.
    admin = os.environ.get("DATABASE_URL") or DEV_DATABASE_URL
    # Its own to this run, too: two gate runs on one Postgres would drop each other's.
    database = run_scoped("agentstack_evals")
    url = rebuild_database(admin, database)
    try:
        # Migrate first: the checkpointer writes into a schema that migrations create,
        # so it is the one piece of infrastructure that comes after them, not before.
        with open_pool(url, min_size=1, max_size=4) as pool:
            migrate.apply(pool, Path(__file__).resolve().parents[1] / "migrations")
            checkpoints, saver = open_checkpointer(url)
            try:
                return work(Database(pool=pool), saver)
            finally:
                checkpoints.close()
    finally:
        drop_database(admin, database)


def _engine(name: str) -> ModelEngine:
    if name == "echo":
        return EchoEngine()
    raise SystemExit(f"unknown engine {name!r}; only 'echo' until E2")


def _behave(engine_name: str) -> int:
    """Run the behaviour cases once on `engine_name` and report. Never a gate: a model's
    proposals are a rate, so a miss here does not fail the command."""
    engine = _engine(engine_name)
    cases = load_behaviour_cases()

    def work(db: Database, saver: Any) -> int:
        passed = 0
        for case in cases:
            outcome = run_behaviour_case(case, db, saver, engine)
            passed += outcome.passed
            print(f"{'pass' if outcome.passed else 'MISS'}  {case.stage:<8}  {case.id}")
            for failure in outcome.failures:
                print(f"        {failure}")
        print(f"\n{passed}/{len(cases)} behaviour cases met their expectations on {engine_name}")
        return 0

    return _on_a_substrate(work)


def _run(cases: list, db: Database, checkpointer: Any) -> int:
    failed = 0
    for case in cases:
        outcome = run_case(case, db, checkpointer)
        mark = "pass" if outcome.passed else "FAIL"
        print(f"{mark}  P{case.part}  {case.id}  ({outcome.seconds * 1000:.0f}ms)")
        for failure in outcome.failures:
            print(f"        {failure}")
        failed += 0 if outcome.passed else 1
    return failed


if __name__ == "__main__":
    sys.exit(main())
