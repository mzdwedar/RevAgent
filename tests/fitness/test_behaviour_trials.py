"""k trials per behaviour case, and what is reported from them (plan E2).

One pass proves little about a sampled model. These tests pin the arithmetic (pass@1,
pass^k, the Wilson interval), the cache that lets a long run resume, and that a seed
reaches the serving system without changing what the adapter reports.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from evals.behaviour import (
    BehaviourCase,
    Trial,
    cost_per_engine,
    engine_for,
    prompt_hash,
    read_trials,
    run_trials,
    summarise,
)
from evals.stats import pass_at_1, pass_hat_k, wilson

from agentstack.model.contract import Usage
from agentstack.model.engine import EchoEngine
from agentstack.model.ollama_engine import OllamaEngine
from agentstack.storage.database import Database
from tests.fitness.test_behaviour_cases import raw_case
from tests.temporal_support import DraftingEngine


def test_wilson_matches_hand_computed_values() -> None:
    low, high = wilson(8, 10)
    assert (round(low, 3), round(high, 3)) == (0.490, 0.943)
    low, high = wilson(0, 5)
    assert low == 0.0 and round(high, 3) == 0.434
    low, high = wilson(5, 5)
    assert high == 1.0 and round(low, 3) == 0.566


def test_wilson_of_no_trials_is_everything() -> None:
    assert wilson(0, 0) == (0.0, 1.0)


def test_pass_at_1_is_the_mean_and_pass_hat_k_is_all_of_them() -> None:
    assert pass_at_1([True, True, False, True]) == 0.75
    assert pass_hat_k([True, True, True]) is True
    assert pass_hat_k([True, False, True]) is False
    assert pass_hat_k([]) is False


def row(case: str, seed: int, passed: bool, *, engine: str = "e", prompt: str = "h") -> Trial:
    return Trial(
        case=case, engine=engine, model="m", seed=seed, prompt_hash=prompt,
        passed=passed, failures=[] if passed else ["x"], seconds=0.1,
    )  # fmt: skip


def test_the_summary_reports_pass_at_1_and_pass_hat_k_per_case_and_overall() -> None:
    trials = [row("a", s, True) for s in range(3)] + [
        row("b", 0, True),
        row("b", 1, False),
        row("b", 2, True),
    ]
    summary = summarise(trials)
    assert summary.per_case["a"].pass_at_1 == 1.0 and summary.per_case["a"].pass_hat_k
    assert round(summary.per_case["b"].pass_at_1, 3) == 0.667
    assert not summary.per_case["b"].pass_hat_k
    assert round(summary.pass_at_1, 3) == 0.833
    assert summary.pass_hat_k == 0.5  # one of two cases passed every trial
    assert summary.trials == 6 and summary.interval[0] < summary.pass_at_1 < summary.interval[1]


def test_engine_names_are_parsed() -> None:
    assert isinstance(engine_for("echo", seed=0), EchoEngine)
    model = engine_for("ollama:qwen3:8b", seed=3, temperature=0.7)
    assert isinstance(model, OllamaEngine)
    assert (model.model, model.seed, model.temperature) == ("qwen3:8b", 3, 0.7)
    with pytest.raises(ValueError, match="unknown engine"):
        engine_for("gpt", seed=0)


def test_a_seed_reaches_the_serving_system_only_when_given() -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        def chat(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs["options"])
            return {"message": {"content": "", "tool_calls": []}}

    from agentstack.model.contract import ModelRequest

    ask = ModelRequest(
        instructions="i", rendered_context="c", exposed_tools=(), max_output_tokens=8
    )
    OllamaEngine(build_client=lambda _: Client()).generate(ask)
    OllamaEngine(seed=4, build_client=lambda _: Client()).generate(ask)
    assert "seed" not in calls[0] and calls[1]["seed"] == 4


def test_the_prompt_hash_moves_with_the_case_and_not_with_the_run() -> None:
    a = BehaviourCase.of(raw_case())
    assert prompt_hash(a) == prompt_hash(BehaviourCase.of(raw_case()))
    assert prompt_hash(a) != prompt_hash(BehaviourCase.of(raw_case(corpus=["more context"])))


def test_trials_are_appended_and_a_second_run_skips_what_is_already_there(
    app_database: Database, checkpointer: Any, tmp_path: Path
) -> None:
    case = BehaviourCase.of(raw_case())
    log = tmp_path / "trials.jsonl"
    built: list[int] = []

    def factory(seed: int) -> DraftingEngine:
        built.append(seed)
        return DraftingEngine()

    first = run_trials([case], factory, engine="stub", k=3, db=app_database,
                       checkpointer=checkpointer, log=log)  # fmt: skip
    assert [t.seed for t in first] == [0, 1, 2] and all(t.passed for t in first)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 3
    assert set(rows[0]) == {
        "case", "engine", "model", "seed", "prompt_hash", "passed", "failures", "seconds",
        "outcome", "fields", "prompt_tokens", "output_tokens", "model_seconds", "proposal",
    }  # fmt: skip
    assert rows[0]["prompt_tokens"] is None  # the stub reports no usage: unknown, not 0
    assert rows[0]["outcome"] == "admitted"
    assert rows[0]["model"] == "drafting-stub" and rows[0]["prompt_hash"] == prompt_hash(case)

    second = run_trials([case], factory, engine="stub", k=5, db=app_database,
                        checkpointer=checkpointer, log=log)  # fmt: skip
    assert built == [0, 1, 2, 3, 4]  # only seeds 3 and 4 were new
    assert [t.seed for t in second] == [0, 1, 2, 3, 4]
    assert len(read_trials(log)) == 5


def test_a_changed_prompt_is_not_served_from_the_cache(
    app_database: Database, checkpointer: Any, tmp_path: Path
) -> None:
    log = tmp_path / "trials.jsonl"
    old = BehaviourCase.of(raw_case())
    new = BehaviourCase.of(raw_case(corpus=["new retrieved text"]))
    run_trials([old], lambda _: DraftingEngine(), engine="stub", k=1, db=app_database,
               checkpointer=checkpointer, log=log)  # fmt: skip
    got = run_trials([new], lambda _: DraftingEngine(), engine="stub", k=1, db=app_database,
                     checkpointer=checkpointer, log=log)  # fmt: skip
    assert len(got) == 1 and len(read_trials(log)) == 2


class _Metered:
    """Wraps an engine and reports a fixed usage on every call."""

    def __init__(self, inner: DraftingEngine) -> None:
        self._inner = inner
        self.asset = inner.asset

    def generate(self, request: Any) -> Any:
        return replace(self._inner.generate(request), usage=Usage(100, 7))


def test_a_trial_records_the_tokens_and_model_latency_the_engine_reported(
    app_database: Database, checkpointer: Any, tmp_path: Path
) -> None:
    case = BehaviourCase.of(raw_case())
    trials = run_trials(
        [case],
        lambda _seed: _Metered(DraftingEngine()),
        engine="m",
        k=1,
        db=app_database,
        checkpointer=checkpointer,
        log=tmp_path / "t.jsonl",
    )
    (trial,) = trials
    # Summed over the turn's model calls, so a multiple of the per-call figure.
    assert trial.prompt_tokens and trial.prompt_tokens % 100 == 0
    assert trial.output_tokens and trial.output_tokens % 7 == 0
    assert trial.model_seconds is not None and 0 <= trial.model_seconds <= trial.seconds


def test_rows_from_before_e4_still_load_and_cost_ignores_what_was_not_reported() -> None:
    old = {"case": "c", "engine": "e", "model": "m", "seed": 0, "prompt_hash": "h",
           "passed": True, "failures": [], "seconds": 0.1}  # fmt: skip
    legacy = Trial(**old)
    assert legacy.prompt_tokens is None
    metered = Trial(
        **old | {"seed": 1, "prompt_tokens": 100, "output_tokens": 10, "model_seconds": 2.0}
    )
    cost = cost_per_engine(
        [
            legacy,
            metered,
            Trial(
                **old | {"seed": 2, "prompt_tokens": 300, "output_tokens": 30, "model_seconds": 4.0}
            ),
        ]
    )["e"]
    assert (cost.prompt_tokens, cost.output_tokens, cost.model_seconds) == (200, 20, 3.0)
    assert cost_per_engine([legacy])["e"].prompt_tokens is None  # unknown, never a median of 0
