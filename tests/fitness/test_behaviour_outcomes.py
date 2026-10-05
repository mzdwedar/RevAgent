"""Every trial is classified from the audit trail and the turn result (plan E3).

The class is read from what the system recorded about the proposal: the audit row the
admission wrote, the turn's refusals, the tool the turn saw proposed. Never from what
the model said about itself, so a model that claims success is classified as it acted.
"""

from __future__ import annotations

from typing import Any

import pytest
from evals.behaviour import (
    ADMITTED,
    DEVIATES,
    MALFORMED,
    NO_PROPOSAL,
    WRONG_TOOL,
    BehaviourCase,
    Trial,
    distribution,
    run_behaviour_case,
)

from agentstack.model.contract import ModelRequest, ModelResponse, ToolCallProposal
from agentstack.storage.database import Database
from tests.fitness.test_behaviour_cases import Blank, Mistyping, Silent, raw_case
from tests.temporal_support import DraftingEngine


class Rewriting(DraftingEngine):
    """Proposes what it was told, with `changes` applied to the arguments."""

    changes: dict[str, Any] = {}
    drop: tuple[str, ...] = ()

    def _answer(self, request: ModelRequest) -> ModelResponse:
        told = super()._answer(request)
        return ModelResponse(
            text="Done, this is exactly the record.",
            proposals=tuple(
                ToolCallProposal(
                    tool=p.tool,
                    arguments={k: v for k, v in p.arguments.items() if k not in self.drop}
                    | self.changes,
                )
                for p in told.proposals
            ),
        )


class WidePercentage(Rewriting):
    changes = {"percentage": 100}


class MissingVariant(Rewriting):
    drop = ("variant",)


class WrongTool(DraftingEngine):
    def _answer(self, _: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="",
            proposals=(ToolCallProposal(tool="roll_out_variant_to_percentage", arguments={}),),
        )


@pytest.fixture
def draft_case() -> BehaviourCase:
    return BehaviourCase.of(raw_case())


@pytest.fixture
def rollout_case() -> BehaviourCase:
    return BehaviourCase.of(
        raw_case(stage="rollout", expect={"tool": "roll_out_variant_to_percentage"})
    )


def classify(case: BehaviourCase, engine: Any, db: Database, checkpointer: Any) -> Any:
    return run_behaviour_case(case, db, checkpointer, engine)


def test_a_good_proposal_is_admitted(
    draft_case: BehaviourCase, app_database: Database, checkpointer: Any
) -> None:
    out = classify(draft_case, DraftingEngine(), app_database, checkpointer)
    assert out.outcome == ADMITTED and out.fields == []


def test_a_proposal_that_differs_from_the_record_names_the_field(
    rollout_case: BehaviourCase, app_database: Database, checkpointer: Any
) -> None:
    out = classify(rollout_case, WidePercentage(), app_database, checkpointer)
    assert out.outcome == DEVIATES
    assert out.fields == ["percentage"]
    assert not out.passed


def test_a_draft_of_another_version_deviates_in_the_version(
    draft_case: BehaviourCase, app_database: Database, checkpointer: Any
) -> None:
    out = classify(draft_case, Mistyping(), app_database, checkpointer)
    assert out.outcome == DEVIATES and out.fields == ["experiment_version"]


def test_a_proposal_that_fails_validation_is_malformed_not_a_deviation(
    draft_case: BehaviourCase, app_database: Database, checkpointer: Any
) -> None:
    out = classify(draft_case, MissingVariant(), app_database, checkpointer)
    assert out.outcome == MALFORMED


def test_another_tool_is_a_wrong_tool(
    draft_case: BehaviourCase, app_database: Database, checkpointer: Any
) -> None:
    out = classify(draft_case, WrongTool(), app_database, checkpointer)
    assert out.outcome == WRONG_TOOL


def test_no_tool_call_is_no_proposal(
    draft_case: BehaviourCase, app_database: Database, checkpointer: Any
) -> None:
    out = classify(draft_case, Silent(), app_database, checkpointer)
    assert out.outcome == NO_PROPOSAL


def test_a_blank_required_field_is_refused_by_validation_as_malformed(
    draft_case: BehaviourCase, app_database: Database, checkpointer: Any
) -> None:
    out = classify(draft_case, Blank(), app_database, checkpointer)
    assert out.outcome == MALFORMED and not out.passed


def test_the_class_does_not_depend_on_what_the_model_says(
    rollout_case: BehaviourCase, app_database: Database, checkpointer: Any
) -> None:
    # `Rewriting` claims "exactly the record" in its text; the audit row says otherwise.
    out = classify(rollout_case, WidePercentage(), app_database, checkpointer)
    assert out.outcome == DEVIATES


def trial(engine: str, outcome: str) -> Trial:
    return Trial("c", engine, "m", 0, "h", outcome == ADMITTED, [], 0.1, outcome=outcome)


def test_the_distribution_is_per_engine() -> None:
    trials = [
        trial("a", ADMITTED),
        trial("a", DEVIATES),
        trial("a", ADMITTED),
        trial("b", MALFORMED),
    ]
    assert distribution(trials) == {
        "a": {ADMITTED: 2, DEVIATES: 1},
        "b": {MALFORMED: 1},
    }
