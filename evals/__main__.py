"""`uv run python -m evals run --gates`."""

from __future__ import annotations

import argparse
import json
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
from evals import checks, grader, labelling, report
from evals.behaviour import (
    OUTCOMES,
    cost_per_engine,
    distribution,
    engine_for,
    load_behaviour_cases,
    read_trials,
    run_trials,
    summarise,
)
from evals.runner import load_cases, run_case


def main() -> int:
    parser = argparse.ArgumentParser(prog="evals")
    parser.add_argument(
        "command",
        choices=[
            "run", "list", "behave", "sample-drafts", "label", "calibrate", "check-drafts",
            "report",
        ],  # fmt: skip
    )
    parser.add_argument("--gates", action="store_true", help="only the release gates")
    parser.add_argument(
        "--engine",
        default="echo",
        help="behave: echo or ollama:<model>; comma-separate to run a matrix",
    )
    parser.add_argument("--k", type=int, default=1, help="behave: trials per case, seeds 0..k-1")
    parser.add_argument("--log", default=".tmp/behaviour.jsonl", help="behave: the JSONL trial log")
    parser.add_argument(
        "--temperature", type=float, default=None, help="behave: override the engine's"
    )
    parser.add_argument(
        "--n", type=int, default=36, help="sample-drafts: drafts drawn from the log"
    )
    parser.add_argument("--seed", type=int, default=0, help="sample-drafts: the sampling seed")
    parser.add_argument("--force", action="store_true", help="sample-drafts: overwrite labels")
    parser.add_argument("--grader", default="ollama:qwen3:8b", help="calibrate: the judge engine")
    parser.add_argument(
        "--out", default="docs/evidence/grader_calibration.json", help="calibrate: the report"
    )
    parser.add_argument("--labels", default=None, help="calibrate: a labels file (default: yours)")
    parser.add_argument("--labeller", default="owner", help="calibrate: who made those labels")
    parser.add_argument(
        "--calibration", default="docs/evidence/grader_calibration_opus_labels_qwen3.json",
        help="report: the calibration whose calibrated criteria may carry a grader score",
    )
    parser.add_argument(
        "--out-csv", default="docs/evidence/model_evals.csv", help="report: the CSV, chart beside"
    )
    args = parser.parse_args()

    if args.command == "report":
        return _report(args)
    if args.command == "check-drafts":
        return _check_drafts(args)
    if args.command == "calibrate":
        return _calibrate(args)
    if args.command == "sample-drafts":
        return _sample_drafts(args)
    if args.command == "label":
        blank = labelling.label()
        print(f"{blank} criteria still blank")
        return 0
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
    engines = [name.strip() for name in args.engine.split(",") if name.strip()]
    try:
        for name in engines:
            engine_for(name, seed=0)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    log = Path(args.log)

    def work(db: Database, saver: Any) -> int:
        every = []
        for name in engines:
            try:
                trials = run_trials(
                    cases,
                    lambda seed, name=name: engine_for(
                        name, seed=seed, temperature=args.temperature
                    ),
                    engine=name,
                    k=args.k,
                    db=db,
                    checkpointer=saver,
                    log=log,
                )
            except ModelUnavailable as exc:
                raise SystemExit(f"{exc}\nStart it: brew services start ollama") from exc
            every.extend(trials)
            summary = summarise(trials)
            for case_id, case in summary.per_case.items():
                mark = "pass" if case.pass_hat_k else "MISS"
                print(f"{mark}  pass@1 {case.pass_at_1:.2f}  {case_id}")
            low, high = summary.interval
            print(
                f"\n{name}, k={args.k}, {summary.trials} trials ({log})\n"
                f"pass@1 {summary.pass_at_1:.2f} (95% Wilson {low:.2f}-{high:.2f})   "
                f"pass^{args.k} {summary.pass_hat_k:.2f} of cases passed every trial\n"
            )
        for name, counts in distribution(every).items():
            total = sum(counts.values())
            shares = "  ".join(
                f"{kind} {counts[kind]}/{total}" for kind in OUTCOMES if kind in counts
            )
            print(f"outcomes, {name}: {shares}")
        for name, cost in cost_per_engine(every).items():
            print(
                f"cost, {name}: median prompt {_fmt(cost.prompt_tokens)} tok, "
                f"output {_fmt(cost.output_tokens)} tok, model {_fmt(cost.model_seconds, 2)}s"
            )
        return 0

    return _on_a_substrate(work)


def _sample_drafts(args: argparse.Namespace) -> int:
    """Draw the labelling set from a trial log. Refuses to overwrite labels already given."""
    if labelling.DRAFTS.exists() and not args.force:
        print(f"{labelling.DRAFTS} exists and may hold your labels; pass --force to replace it")
        return 1
    items, key = labelling.sample_drafts(
        read_trials(Path(args.log)), load_behaviour_cases(), n=args.n, seed=args.seed
    )
    labelling.write_jsonl(labelling.DRAFTS, items)
    labelling.write_jsonl(labelling.KEY, key)
    print(f"{len(items)} items in {labelling.DRAFTS}; key (engine hidden) in {labelling.KEY}")
    return 0


def _check_drafts(args: argparse.Namespace) -> int:
    """Run the deterministic C3/C4 checks over the labelled drafts and compare with the labels."""
    labels = Path(args.labels) if args.labels else labelling.DRAFTS
    try:
        report = checks.run_checks(labels=labels, labeller=args.labeller)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    for name in ("C3", "C4"):
        result, veto = report["criteria"][name], report["veto"][name]
        kappa = "n/a" if result["kappa"] is None else f"{result['kappa']:.2f}"
        precision = "n/a" if veto["precision"] is None else f"{veto['precision']:.2f}"
        recall = "n/a" if veto["recall"] is None else f"{veto['recall']:.2f}"
        print(f"{name}  kappa {kappa}  precision {precision}  recall {recall}  {result['status']}")
    print(f"report: {out}")
    return 0


def _report(args: argparse.Namespace) -> int:
    """Turn a trial log into the committed CSV and chart. Reads only; runs no model."""
    log = Path(args.log)
    trials = read_trials(log)
    if not trials:
        raise SystemExit(f"no trials in {log}")
    calibration = Path(args.calibration)
    table = report.rows(trials, calibration)
    out = Path(args.out_csv)
    report.write_csv(table, out)
    report.write_chart(table, out.with_suffix(".png"))
    for row in table:
        print(
            f"{row['engine']}: pass@1 {row['pass_at_1']} ({row['ci_low']}-{row['ci_high']}), "
            f"pass^k {row['pass_hat_k']}, graded criteria: {row['calibrated_criteria']}"
        )
    print(f"report: {out}")
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    """Grade the labelled drafts and write how far the grader can be trusted, per criterion."""
    try:
        report = grader.run_calibration(
            engine_for(args.grader, seed=0, temperature=0.0),
            labels=Path(args.labels) if args.labels else labelling.DRAFTS,
            labeller=args.labeller,
        )
    except (ValueError, ModelUnavailable) as exc:
        raise SystemExit(str(exc)) from exc
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    for name, result in report["criteria"].items():
        kappa = "n/a" if result["kappa"] is None else f"{result['kappa']:.2f}"
        print(f"{name}  kappa {kappa}  n={result['n']}  {result['status']}")
    print(f"injections graded better than labelled: {report['injections']['passes'] is False}")
    print(f"report: {out}")
    return 0


def _fmt(value: float | None, digits: int = 0) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


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
