"""E7: the evidence report is a pure function of the trial log, pinned by a golden CSV."""

from __future__ import annotations

import json
from pathlib import Path

from evals.behaviour import Trial
from evals.report import rows, write_chart, write_csv


def trial(
    engine: str, case: str, seed: int, passed: bool, outcome: str, tokens: int | None
) -> Trial:
    return Trial(
        case=case, engine=engine, model="m", seed=seed, prompt_hash="h", passed=passed,
        failures=[] if passed else ["x"], seconds=1.0, outcome=outcome,
        prompt_tokens=tokens, output_tokens=tokens and tokens // 10, model_seconds=2.0,
    )  # fmt: skip


LOG = [
    trial("a", "c1", 0, True, "admitted", 100),
    trial("a", "c1", 1, True, "admitted", 300),
    trial("a", "c2", 0, True, "admitted", 200),
    trial("a", "c2", 1, False, "deviates", 200),
    trial("b", "c1", 0, False, "no_proposal", None),
]

GOLDEN = """\
engine,trials,cases,pass_at_1,ci_low,ci_high,pass_hat_k,admitted,deviates,malformed,wrong_tool,no_proposal,median_prompt_tokens,median_output_tokens,median_model_seconds,calibrated_criteria
a,4,2,0.75,0.301,0.954,0.5,0.75,0.25,0.0,0.0,0.0,200.0,20.0,2.0,none
b,1,1,0.0,0.0,0.793,0.0,0.0,0.0,0.0,0.0,1.0,,,2.0,none
"""


def test_the_csv_matches_the_golden_and_missing_tokens_stay_blank(tmp_path: Path) -> None:
    out = tmp_path / "m.csv"
    write_csv(rows(LOG), out)
    assert out.read_text() == GOLDEN


def test_a_score_is_claimed_only_for_calibrated_criteria(tmp_path: Path) -> None:
    calibration = tmp_path / "c.json"
    calibration.write_text(json.dumps({"calibrated": ["C2"], "human_only": ["C1"]}))
    assert {r["calibrated_criteria"] for r in rows(LOG, calibration)} == {"C2"}
    assert {r["calibrated_criteria"] for r in rows(LOG, tmp_path / "missing.json")} == {"none"}


def test_the_chart_is_written(tmp_path: Path) -> None:
    write_chart(rows(LOG), tmp_path / "m.png")
    assert (tmp_path / "m.png").stat().st_size > 1000
