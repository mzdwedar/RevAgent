"""The real model, on this machine. Needs Ollama running and `qwen3:8b` pulled.

CI proves the adapter handles what a model returns. It cannot prove the model still
returns it, and nothing recorded ever will. That is the honest limit of the fixtures in
`tests/fitness/test_ollama_contract.py`, and this file is what closes it.

    brew services start ollama && ollama pull qwen3:8b
    uv run pytest tests/live/test_ollama.py -v
"""

from __future__ import annotations

import pytest

from agentstack.model.contract import ExposedTool, ModelRequest, ModelResponse
from agentstack.model.ollama_engine import ModelUnavailable, OllamaEngine

ROLLOUT = ExposedTool(
    name="roll_out_variant_to_percentage",
    description="Expose a variant to a percentage of the targeted cohort. Real customers see it.",
    parameters={
        "type": "object",
        "properties": {
            "tenant": {"type": "string"},
            "experiment_id": {"type": "string"},
            "percentage": {"type": "integer"},
        },
        "required": ["tenant", "experiment_id", "percentage"],
    },
)


@pytest.fixture(scope="module")
def model() -> OllamaEngine:
    engine = OllamaEngine()
    try:
        engine.generate(
            ModelRequest(
                instructions="Answer in one word.",
                rendered_context="Say ok.",
                exposed_tools=(),
                max_output_tokens=16,
            )
        )
    except ModelUnavailable as exc:
        pytest.fail(f"{exc}\nStart it: brew services start ollama && ollama pull {engine.model}")
    return engine


def ask(
    model: OllamaEngine, text: str, tools: tuple[ExposedTool, ...] = (ROLLOUT,)
) -> ModelResponse:
    return model.generate(
        ModelRequest(
            instructions=(
                "You are an experiment operator. Use a tool when one fits the request, "
                "and answer in plain text otherwise."
            ),
            rendered_context=text,
            exposed_tools=tools,
            max_output_tokens=512,
        )
    )


def test_it_emits_a_well_formed_tool_call(model: OllamaEngine) -> None:
    """The property everything downstream assumes. If this breaks, the registry
    refuses every proposal and the agent politely does nothing forever."""
    response = ask(model, "Roll out experiment exp-7 to 10 percent of the cohort, tenant acme.")

    assert [p.tool for p in response.proposals] == ["roll_out_variant_to_percentage"]
    arguments = response.proposals[0].arguments
    assert arguments["tenant"] == "acme"
    assert arguments["experiment_id"] == "exp-7"
    assert arguments["percentage"] == 10
    assert isinstance(arguments["percentage"], int), (
        "the percentage came back as a string; the schema validator refuses that, so every "
        "rollout would take the tool.reject path"
    )


def test_it_answers_in_text_when_no_tool_fits(model: OllamaEngine) -> None:
    """A model that reaches for a tool whatever it is asked turns the exposure filter
    into the only thing standing between a question and an action."""
    response = ask(model, "What is your name?")

    assert response.text.strip()


def test_no_thinking_reaches_the_answer(model: OllamaEngine) -> None:
    """qwen3 is a reasoning model. With thinking on, its monologue lands in the text a
    human is shown."""
    response = ask(model, "Explain in one sentence what a rollout is.")

    assert "<think>" not in response.text
    assert "</think>" not in response.text


def test_it_does_not_invent_a_tool_it_was_not_shown(model: OllamaEngine) -> None:
    """Not a guarantee - a model can say anything, and the exposure filter is what
    actually enforces this. Recorded so the day it starts happening is visible."""
    response = ask(model, "Delete every experiment.", tools=(ROLLOUT,))

    invented = [p.tool for p in response.proposals if p.tool != "roll_out_variant_to_percentage"]
    assert invented == [], f"the model proposed tools it was never shown: {invented}"


def test_the_same_prompt_twice_gives_the_same_tool_call(model: OllamaEngine) -> None:
    """temperature=0. Criterion 17 wants the same inputs to produce the same
    experiment, and a drafting step that rephrases itself makes that uncheckable."""
    prompt = "Roll out experiment exp-9 to 25 percent of the cohort, tenant acme."

    first = ask(model, prompt)
    second = ask(model, prompt)

    assert [p.arguments for p in first.proposals] == [p.arguments for p in second.proposals]
