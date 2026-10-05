"""One behaviour case, one trial, on the real model (plan E2). Needs Ollama and `qwen3:8b`.

    uv run pytest tests/live/test_behaviour_live.py -v

The grade is not asserted: a model may miss. What is asserted is that a real trial runs
end to end, is logged, and is served from the log the second time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from evals.behaviour import engine_for, load_behaviour_cases, read_trials, run_trials

from agentstack.model.ollama_engine import ModelUnavailable
from agentstack.storage.database import Database


def test_one_real_trial_is_logged_and_then_cached(
    app_database: Database, checkpointer: Any, tmp_path: Path
) -> None:
    case = next(c for c in load_behaviour_cases() if c.id == "rollout-usd-cohort")
    log = tmp_path / "live.jsonl"

    def run() -> int:
        return len(
            run_trials(
                [case],
                lambda seed: engine_for("ollama:qwen3:8b", seed=seed),
                engine="ollama:qwen3:8b",
                k=1,
                db=app_database,
                checkpointer=checkpointer,
                log=log,
            )
        )

    try:
        assert run() == 1
    except ModelUnavailable as exc:
        pytest.fail(f"{exc}\nStart it: brew services start ollama && ollama pull qwen3:8b")
    trials = read_trials(log)
    assert len(trials) == 1 and trials[0].model == "qwen3:8b" and trials[0].seconds > 0
    assert run() == 1 and len(read_trials(log)) == 1
