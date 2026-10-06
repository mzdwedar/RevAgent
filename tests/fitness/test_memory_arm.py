"""E8: the memory arm shows a case its prior results; the other arm does not. The measures
are read off the proposal and the case's known priors, with no model judging anything."""

from __future__ import annotations

from typing import Any

from evals.behaviour import Trial, load_memory_cases, run_behaviour_case
from evals.report import cites, memory_rows

from agentstack.model.contract import ModelRequest, ModelResponse
from agentstack.storage.database import Database
from tests.temporal_support import DraftingEngine


class Watching(DraftingEngine):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def _answer(self, request: ModelRequest) -> ModelResponse:
        self.seen.append(request.rendered_context)
        return super()._answer(request)


def test_only_the_memory_arm_is_shown_its_prior_results(
    app_database: Database, checkpointer: Any
) -> None:
    case = next(c for c in load_memory_cases() if c.id == "memory-one-loser")
    off, on = Watching(), Watching()
    run_behaviour_case(case, app_database, checkpointer, off, memory=False)
    run_behaviour_case(case, app_database, checkpointer, on, memory=True)

    prior = case.learned[0]["experiment_id"]
    assert prior not in " ".join(off.seen), "the no-memory arm saw a prior result"
    assert prior in " ".join(on.seen) and "decision: stop" in " ".join(on.seen)


def test_every_memory_case_has_a_known_prior_and_unique_ids() -> None:
    cases = load_memory_cases()
    ids = [s["experiment_id"] for c in cases for s in c.learned]
    assert len(cases) >= 4 and len(ids) == len(set(ids))
    assert all(
        s["readout"]["offer"] and "insv_cents" in s["readout"] for c in cases for s in c.learned
    )


def test_citing_means_naming_a_prior_experiment_not_a_prefix_of_one() -> None:
    learned = [{"experiment_id": "exp-m1-p1", "experiment_version": "exp:m1p1v1"}]
    assert cites("Unlike exp-m1-p1, this tries a smaller offer.", learned)
    assert cites("see exp:m1p1v1", learned)
    assert not cites("an offer like exp-m1-p10 never ran", learned)
    assert not cites("a cheaper offer should retain them", learned)


def trial(arm: str, variant: str | None, hypothesis: str, outcome: str = "admitted") -> Trial:
    proposal = (
        None
        if variant is None
        else {"tool": "t", "arguments": {"variant": variant, "hypothesis": hypothesis}}
    )
    return Trial(
        case="memory-two-losers-one-winner", engine="e", model="m", seed=0, prompt_hash="h",
        passed=True, failures=[], seconds=0.1, outcome=outcome, proposal=proposal, arm=arm,
    )  # fmt: skip


def test_the_arms_are_scored_against_the_same_priors() -> None:
    log = [
        trial("no_memory", "free-month", "a good offer"),
        trial("no_memory", "10-percent-off", "cheap"),
        trial("memory", "10-percent-off", "Unlike exp-m2-p1 this wins.", "deviates"),
        trial("memory", None, "", "no_proposal"),
    ]
    off, on = memory_rows(log, load_memory_cases())
    assert (off["arm"], on["arm"]) == ("no_memory", "memory")
    assert (off["repeats_a_loser"], off["picks_a_winner"]) == (0.5, 0.5)
    assert (on["repeats_a_loser"], on["picks_a_winner"]) == (0.0, 1.0)
    assert (off["cites_prior_evidence"], on["cites_prior_evidence"]) == (0.0, 0.5)
    assert (off["refused"], on["refused"]) == (0.0, 1.0)
    assert on["chose_an_offer"] == 0.5
