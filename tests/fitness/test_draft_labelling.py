"""The draft labelling set (plan E5): stratified, deterministic, blind, and valid.

The labels are the ground truth E6 calibrates a grader against, so how the set is drawn
matters: a sample skewed to one engine, or one that shows the labeller which engine wrote
a draft, would bias the number everything downstream rests on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.behaviour import BehaviourCase, Trial
from evals.labelling import (
    CRITERIA,
    INJECTIONS,
    LabelsInvalid,
    draft_candidates,
    label,
    read_jsonl,
    sample_drafts,
    validate_labels,
    write_jsonl,
)

from tests.fitness.test_behaviour_cases import raw_case

ENGINES = ("ollama:a", "ollama:b", "ollama:c")


def _arguments(hypothesis: str) -> dict[str, str]:
    return {"hypothesis": hypothesis, "variant": "v " + hypothesis}


def trial(
    engine: str, seed: int, hypothesis: str, *, tool: str = "create_experiment_draft"
) -> Trial:
    return Trial(
        case="c1", engine=engine, model="m", seed=seed, prompt_hash="h", passed=True,
        failures=[], seconds=0.1,
        proposal={"tool": tool, "arguments": _arguments(hypothesis)},
    )  # fmt: skip


def log() -> list[Trial]:
    return [trial(e, s, f"idea {ENGINES.index(e)}-{s}") for e in ENGINES for s in range(20)]


def cases() -> list[BehaviourCase]:
    return [BehaviourCase.of(raw_case(id="c1"))]


def test_the_sample_is_stratified_by_engine_and_adds_the_injections() -> None:
    items, key = sample_drafts(log(), cases(), n=36, seed=1)
    assert len(items) == 40 == len(key)
    engines = [k["engine"] for k in key if "engine" in k]
    assert {e: engines.count(e) for e in ENGINES} == {e: 12 for e in ENGINES}
    assert sum(1 for k in key if k.get("injection")) == len(INJECTIONS) == 4


def test_a_seed_gives_the_same_set_and_another_seed_a_different_one() -> None:
    first = sample_drafts(log(), cases(), n=36, seed=7)
    assert sample_drafts(log(), cases(), n=36, seed=7) == first
    assert sample_drafts(log(), cases(), n=36, seed=8) != first


def test_the_labeller_cannot_see_the_engine_or_the_case() -> None:
    items, key = sample_drafts(log(), cases(), n=36, seed=1)
    shown = json.dumps(items)
    assert "ollama" not in shown and '"case"' not in shown and "experiment_version" not in shown
    # The order is shuffled: engines are not in contiguous runs.
    order = [k.get("engine", "injection") for k in key]
    assert order != sorted(order)


def test_a_draft_that_was_not_a_draft_is_not_sampled() -> None:
    trials = [
        trial("ollama:a", 0, "real"),
        trial("ollama:a", 1, "wrong tool", tool="roll_out_variant_to_percentage"),
        trial("ollama:a", 2, "   "),
        Trial("c1", "ollama:a", "m", 3, "h", False, ["x"], 0.1),  # an old row, no proposal
    ]
    assert [c.draft["hypothesis"] for c in draft_candidates(trials)] == ["real"]


def test_identical_text_from_one_engine_is_labelled_once() -> None:
    repeats = [trial("ollama:a", s, "same words") for s in range(5)]
    assert len(draft_candidates(repeats)) == 1


def test_too_few_drafts_for_an_engine_is_an_error_not_a_short_sample() -> None:
    few = [trial(e, s, f"idea {ENGINES.index(e)}-{s}") for e in ENGINES for s in range(3)]
    with pytest.raises(LabelsInvalid, match="distinct drafts"):
        sample_drafts(few, cases(), n=36)


def test_validation_accepts_blank_and_rubric_verdicts_and_nothing_else() -> None:
    items, _ = sample_drafts(log(), cases(), n=36, seed=1)
    validate_labels(items)
    items[0]["labels"] = {c: "yes" for c in CRITERIA} | {"C2": "unsure", "C3": None}
    validate_labels(items)
    for bad in (
        {**items[1]["labels"], "C1": "maybe"},
        {"C1": "yes"},
        {**items[1]["labels"], "C6": "yes"},
    ):
        items[1]["labels"] = bad
        with pytest.raises(LabelsInvalid):
            validate_labels(items)


def test_the_rubric_names_exactly_the_criteria_the_labels_use() -> None:
    rubric = (Path(__file__).resolve().parents[2] / "evals/rubrics/draft.md").read_text()
    headings = [line[3:] for line in rubric.splitlines() if line.startswith("## C")]
    assert headings == [f"{c} {name}" for c, name in CRITERIA.items()]
    assert rubric.count("**Pass:**") == rubric.count("**Fail:**") == len(CRITERIA)


def test_labelling_saves_as_it_goes_and_resumes_where_it_stopped(tmp_path: Path) -> None:
    items, _ = sample_drafts(log(), cases(), n=36, seed=1)
    path = tmp_path / "drafts.jsonl"
    write_jsonl(path, items[:2])
    answers = iter(["y", "n", "?", "y", "n", "C2: borderline", "q"])
    blank = label(path, ask=lambda _prompt: next(answers), show=lambda _line: None)
    saved = read_jsonl(path)
    assert saved[0]["labels"] == {"C1": "yes", "C2": "no", "C3": "unsure", "C4": "yes", "C5": "no"}
    assert saved[0]["notes"] == {"C2": "borderline"}
    assert saved[1]["labels"] == dict.fromkeys(CRITERIA)  # the 'q' left it untouched
    assert blank == 5

    answers = iter(["y"] * 5 + [""])
    assert label(path, ask=lambda _prompt: next(answers), show=lambda _line: None) == 0
    assert read_jsonl(path)[0]["labels"]["C2"] == "no"  # not asked again
