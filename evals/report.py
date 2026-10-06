"""The evidence report: one row per engine from a behaviour trial log (plan E7).

Pure over the log, so a golden test pins it. Nothing here runs a model. Grader scores are
reported only for criteria the grader was calibrated on; when none were, the report says
so instead of printing a score nobody can trust.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evals.behaviour import OUTCOMES, BehaviourCase, Trial, cost_per_engine, summarise

COLUMNS = (
    "engine", "trials", "cases", "pass_at_1", "ci_low", "ci_high", "pass_hat_k",
    *OUTCOMES, "median_prompt_tokens", "median_output_tokens", "median_model_seconds",
    "calibrated_criteria",
)  # fmt: skip


def calibrated(calibration: Path | None) -> str:
    """The criteria a grader may score, or `none` (all human-only)."""
    if calibration is None or not calibration.exists():
        return "none"
    names = json.loads(calibration.read_text()).get("calibrated", [])
    return " ".join(names) or "none"


def rows(trials: Sequence[Trial], calibration: Path | None = None) -> list[dict[str, Any]]:
    cost = cost_per_engine(trials)
    graded = calibrated(calibration)
    out = []
    for engine in sorted({t.engine for t in trials}):
        mine = [t for t in trials if t.engine == engine]
        summary = summarise(mine)
        row: dict[str, Any] = {
            "engine": engine,
            "trials": summary.trials,
            "cases": len(summary.per_case),
            "pass_at_1": round(summary.pass_at_1, 3),
            "ci_low": round(summary.interval[0], 3),
            "ci_high": round(summary.interval[1], 3),
            "pass_hat_k": round(summary.pass_hat_k, 3),
        }
        known = [t for t in mine if t.outcome]
        for kind in OUTCOMES:
            row[kind] = (
                round(sum(t.outcome == kind for t in known) / len(known), 3) if known else ""
            )
        for name, value in (
            ("median_prompt_tokens", cost[engine].prompt_tokens),
            ("median_output_tokens", cost[engine].output_tokens),
            ("median_model_seconds", cost[engine].model_seconds),
        ):
            row[name] = "" if value is None else round(value, 2)
        row["calibrated_criteria"] = graded
        out.append(row)
    return out


def write_csv(report: Sequence[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(report)


def write_chart(report: Sequence[dict[str, Any]], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = [r["engine"].removeprefix("ollama:") for r in report]
    mean = [r["pass_at_1"] for r in report]
    err = [
        [r["pass_at_1"] - r["ci_low"] for r in report],
        [r["ci_high"] - r["pass_at_1"] for r in report],
    ]
    fig, ax = plt.subplots(figsize=(6, 3.5))
    ax.bar(names, mean, yerr=err, capsize=4, label="pass@1 (95% Wilson)")
    ax.plot(names, [r["pass_hat_k"] for r in report], "k_", ms=24, label="pass^k (share of cases)")
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("share")
    ax.set_title("Behaviour cases per engine")
    ax.legend(loc="lower right", fontsize=8)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)


# E8: the same cases, with and without their prior results recalled. Everything here is
# read off the proposal and the case's known priors; no model judges anything.
MEMORY_COLUMNS = (
    "engine", "arm", "trials", "refused", "chose_an_offer", "cites_prior_evidence",
    "repeats_a_loser", "picks_a_winner",
)  # fmt: skip


def cites(hypothesis: str, learned: Sequence[dict[str, Any]]) -> bool:
    """The hypothesis names a prior experiment (its id or version) or a readout id."""
    return any(
        re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", hypothesis)
        for spec in learned
        for name in (spec["experiment_id"], spec["experiment_version"])
    )


def memory_rows(trials: Sequence[Trial], cases: Sequence[BehaviourCase]) -> list[dict[str, Any]]:
    """Per engine and arm. A "loser" is an offer whose prior INSV was not positive, a
    "winner" one whose was. Both are judged against the same priors in both arms, so the
    no-memory arm shows what chance and the model's habits give."""
    by_case = {c.id: c for c in cases}
    out = []
    for engine in sorted({t.engine for t in trials}):
        for arm in ("no_memory", "memory"):
            mine = [t for t in trials if t.engine == engine and t.arm == arm and t.case in by_case]
            if not mine:
                continue
            chose = cited = lost = won = 0
            for t in mine:
                args = (t.proposal or {}).get("arguments", {})
                offer = args.get("variant")
                learned = by_case[t.case].learned
                priors = {s["readout"]["offer"]: s["readout"]["insv_cents"] for s in learned}
                if offer:
                    chose += 1
                    lost += offer in priors and priors[offer] <= 0
                    won += offer in priors and priors[offer] > 0
                cited += cites(str(args.get("hypothesis", "")), learned)
            n = len(mine)
            out.append(
                {
                    "engine": engine, "arm": arm, "trials": n,
                    "refused": round(sum(t.outcome != "admitted" for t in mine) / n, 3),
                    "chose_an_offer": round(chose / n, 3),
                    "cites_prior_evidence": round(cited / n, 3),
                    "repeats_a_loser": round(lost / chose, 3) if chose else "",
                    "picks_a_winner": round(won / chose, 3) if chose else "",
                }
            )  # fmt: skip
    return out


def write_memory_csv(report: Sequence[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MEMORY_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(report)
