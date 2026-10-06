"""The evidence report: one row per engine from a behaviour trial log (plan E7).

Pure over the log, so a golden test pins it. Nothing here runs a model. Grader scores are
reported only for criteria the grader was calibrated on; when none were, the report says
so instead of printing a score nobody can trust.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evals.behaviour import OUTCOMES, Trial, cost_per_engine, summarise

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
