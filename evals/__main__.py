"""`uv run python -m evals run --gates`."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentstack.model.ollama_engine import ModelUnavailable
from agentstack.storage import migrate
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import DEV_DATABASE_URL, open_pool
from agentstack.storage.provision import drop_database, rebuild_database, run_scoped
from evals.behaviour import (
    OUTCOMES,
    distribution,
    engine_for,
    load_behaviour_cases,
    run_trials,
    summarise,
)
from evals.runner import load_cases, run_case


def main() -> int:
    parser = argparse.ArgumentParser(prog="evals")
    parser.add_argument("command", choices=["run", "list", "behave"])
    parser.add_argument("--gates", action="store_true", help="only the release gates")
    parser.add_argument("--engine", default="echo", help="behave: echo or ollama:<model>")
    parser.add_argument("--k", type=int, default=1, help="behave: trials per case, seeds 0..k-1")
    parser.add_argument("--log", default=".tmp/behaviour.jsonl", help="behave: the JSONL trial log")
    parser.add_argument(
        "--temperature", type=float, default=None, help="behave: override the engine's"
    )
    args = parser.parse_args()

    if args.command == "behave":
        return _behave(args)

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


def _behave(args: argparse.Namespace) -> int:
    """Run the behaviour cases k times on one engine and report. Never a gate: a model's
    proposals are a rate, so a miss here does not fail the command."""
    cases = load_behaviour_cases()
    try:
        engine_for(args.engine, seed=0)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    log = Path(args.log)

    def work(db: Database, saver: Any) -> int:
        try:
            trials = run_trials(
                cases,
                lambda seed: engine_for(args.engine, seed=seed, temperature=args.temperature),
                engine=args.engine,
                k=args.k,
                db=db,
                checkpointer=saver,
                log=log,
            )
        except ModelUnavailable as exc:
            raise SystemExit(f"{exc}\nStart it: brew services start ollama") from exc
        summary = summarise(trials)
        for case_id, case in summary.per_case.items():
            mark = "pass" if case.pass_hat_k else "MISS"
            print(f"{mark}  pass@1 {case.pass_at_1:.2f}  {case_id}")
        low, high = summary.interval
        print(
            f"\n{args.engine}, k={args.k}, {summary.trials} trials ({log})\n"
            f"pass@1 {summary.pass_at_1:.2f} (95% Wilson {low:.2f}-{high:.2f})   "
            f"pass^{args.k} {summary.pass_hat_k:.2f} of cases passed every trial"
        )
        for name, counts in distribution(trials).items():
            total = sum(counts.values())
            shares = "  ".join(
                f"{kind} {counts[kind]}/{total}" for kind in OUTCOMES if kind in counts
            )
            print(f"outcomes, {name}: {shares}")
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
