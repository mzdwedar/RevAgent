"""The Ollama adapter's half of the interaction contract (Model engine & inference).

What the adapter owes the layers above it: report faithfully what the model said, and
nothing more. The tool name may not be one that was offered and the arguments may be
the wrong shape - both are someone else's problem by design, because the registry
validates against the schema and the exposure filter refuses a tool this run was never
shown. An adapter that "helpfully" corrected either would be deciding.

These run without Ollama installed. The real model is exercised in `tests/live`, which
needs a machine with it running - the same split as TabPFN, and for the same reason: CI
proves our code handles the output, not that the model still produces it.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, handle
from agentstack.model.contract import ExposedTool, ModelRequest
from agentstack.model.ollama_engine import (
    DEFAULT_NUM_CTX,
    ModelUnavailable,
    OllamaEngine,
)
from agentstack.runtime.run import Run

from .conftest import ROLLOUT_ARGS, SCOPES, TENANT, USER

ROLLOUT_TOOL = ExposedTool(
    name="roll_out_variant_to_percentage",
    description="Expose a variant to a percentage of the targeted cohort.",
    parameters={
        "type": "object",
        "properties": {"tenant": {"type": "string"}, "percentage": {"type": "integer"}},
        "required": ["tenant", "percentage"],
    },
)


class FakeClient:
    """Records the call and returns a canned answer, in the shape ollama returns."""

    def __init__(self, host: str, answer: dict[str, Any] | None = None) -> None:
        # The host is recorded, not ignored: the seam hands it over, and a factory that
        # quietly connected somewhere else is worth being able to catch.
        self.host = host
        self.answer = answer or {"message": {"content": "nothing to do", "tool_calls": []}}
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.answer


def engine(answer: dict[str, Any] | None = None) -> tuple[OllamaEngine, FakeClient]:
    holder: dict[str, FakeClient] = {}

    def build(host: str) -> FakeClient:
        holder.setdefault("client", FakeClient(host, answer))
        return holder["client"]

    model = OllamaEngine(build_client=build)
    return model, build(model.host)


def request(tools: tuple[ExposedTool, ...] = (ROLLOUT_TOOL,)) -> ModelRequest:
    return ModelRequest(
        instructions="You are a support agent.",
        rendered_context="roll out exp-7 to 10 percent",
        exposed_tools=tools,
        max_output_tokens=256,
    )


# --- the settings that are decisions, not defaults ---


def test_the_context_window_is_set_explicitly() -> None:
    """Ollama's own default is smaller than most people assume. A context window
    discovered by watching answers get worse is a serving-system setting mistaken for a
    property of the model."""
    model, client = engine()

    model.generate(request())

    assert client.calls[0]["options"]["num_ctx"] == DEFAULT_NUM_CTX
    assert DEFAULT_NUM_CTX == 8192


def test_the_context_window_is_reported_as_the_asset_window() -> None:
    """Model engine & inference: the asset is described separately from whoever serves it, and the
    two
    must not disagree about how much it can be told."""
    model, _ = engine()

    assert model.asset.context_window == model.num_ctx
    assert model.asset.name == model.model


def test_thinking_is_disabled() -> None:
    """qwen3 is a reasoning model. Its thinking is not an answer, and leaving it on
    puts a monologue in the text the turn returns to a human."""
    model, client = engine()

    model.generate(request())

    assert client.calls[0]["think"] is False


def test_sampling_is_deterministic_by_default() -> None:
    """Criterion 17 wants the same inputs to produce the same experiment. A drafting
    step that rephrases itself each run makes an `experiment_version` covering the
    model worth nothing."""
    model, client = engine()

    model.generate(request())

    assert client.calls[0]["options"]["temperature"] == 0.0


def test_the_turn_s_output_budget_is_passed_through_not_the_engine_s() -> None:
    model, client = engine()

    model.generate(request())

    assert client.calls[0]["options"]["num_predict"] == 256


# --- the menu ---


def test_the_model_is_shown_the_schemas_not_just_the_names() -> None:
    model, client = engine()

    model.generate(request())

    tools = client.calls[0]["tools"]
    assert [t["function"]["name"] for t in tools] == ["roll_out_variant_to_percentage"]
    assert tools[0]["function"]["parameters"]["required"] == ["tenant", "percentage"]


def test_the_model_is_not_shown_the_authority_metadata() -> None:
    """`ExposedTool` is a name, a description and a parameter schema. A `ToolSpec` also
    carries the scope, the surface, the approval tier and the idempotency policy, and
    handing those over would be handing the model the authority metadata to reason
    about."""
    assert set(ExposedTool.__dataclass_fields__) == {"name", "description", "parameters"}


def test_an_empty_menu_is_still_a_valid_call() -> None:
    model, client = engine()

    model.generate(request(tools=()))

    assert client.calls[0]["tools"] == []


# --- what comes back is a proposal ---


def test_a_tool_call_becomes_a_proposal() -> None:
    model, _ = engine(
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "roll_out_variant_to_percentage",
                            "arguments": {"tenant": "acme"},
                        }
                    }
                ],
            }
        }
    )

    response = model.generate(request())

    assert [p.tool for p in response.proposals] == ["roll_out_variant_to_percentage"]
    assert response.proposals[0].arguments == {"tenant": "acme"}


def test_a_tool_that_was_never_offered_is_reported_faithfully() -> None:
    """The adapter does not filter. The exposure filter refuses it a layer up, where
    the refusal is recorded as evidence rather than silently swallowed here."""
    model, _ = engine(
        {"message": {"content": "", "tool_calls": [{"function": {"name": "drop_database"}}]}}
    )

    response = model.generate(request())

    assert [p.tool for p in response.proposals] == ["drop_database"]


def test_malformed_arguments_are_reported_faithfully() -> None:
    """An adapter that corrected the type would be deciding. Validation belongs to the
    registry, against the schema, where a refusal leaves a `tool.reject` span."""
    model, _ = engine(
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "roll_out_variant_to_percentage",
                            "arguments": {"percentage": "lots"},
                        }
                    }
                ],
            }
        }
    )

    response = model.generate(request())

    assert response.proposals[0].arguments == {"percentage": "lots"}


def test_a_tool_call_with_no_name_is_dropped_and_the_turn_still_answers() -> None:
    """A call naming nothing is not a proposal about anything, and it must not take the
    model's text down with it."""
    model, _ = engine(
        {"message": {"content": "I could not help", "tool_calls": [{"function": {}}]}}
    )

    response = model.generate(request())

    assert response.proposals == ()
    assert response.text == "I could not help"


def test_an_object_shaped_answer_reads_the_same_as_a_dict() -> None:
    """The real client returns pydantic models; fixtures and fakes return dicts."""

    class Function:
        name = "roll_out_variant_to_percentage"
        arguments = {"tenant": "acme"}

    class Call:
        function = Function()

    class Message:
        content = "ok"
        tool_calls = [Call()]

    class Answer:
        message = Message()

    model = OllamaEngine(build_client=lambda host: _ClientReturning(host, Answer()))

    response = model.generate(request())

    assert response.text == "ok"
    assert [p.tool for p in response.proposals] == ["roll_out_variant_to_percentage"]


class _ClientReturning:
    def __init__(self, host: str, answer: Any) -> None:
        self.host = host
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.answer


# --- when the serving system is not there ---


class Unreachable:
    """A serving system that refuses the connection, naming the host it refused."""

    def __init__(self, host: str) -> None:
        self.host = host

    def chat(self, **kwargs: Any) -> Any:
        raise OSError(f"connection refused by {self.host} for {kwargs.get('model')}")


def test_an_unreachable_model_stops_the_turn_rather_than_continuing_without_one() -> None:
    model = OllamaEngine(build_client=lambda host: Unreachable(host))

    with pytest.raises(ModelUnavailable, match="could not answer"):
        model.generate(request())


def test_the_failure_names_the_model_and_the_host() -> None:
    """An operator reading this needs to know which serving system was unreachable."""

    model = OllamaEngine(
        model="qwen3:8b",
        host="http://elsewhere:11434",
        build_client=lambda host: Unreachable(host),
    )

    with pytest.raises(ModelUnavailable) as caught:
        model.generate(request())

    assert "qwen3:8b" in str(caught.value)
    assert "http://elsewhere:11434" in str(caught.value)


# --- the acceptance, through the whole stack ---


def test_a_malformed_proposal_is_rejected_and_the_turn_still_answers(
    stack: Stack, run: Run
) -> None:
    """The existing `tool.reject` path, now driven by a real adapter.

    The model asked for a rollout with the percentage as prose. The registry refuses it
    against the schema, the refusal is traced, and the turn answers instead of raising.
    """
    stack.deps.engine = OllamaEngine(
        build_client=lambda host: FakeClient(
            host,
            {
                "message": {
                    "content": "rolling out",
                    "tool_calls": [
                        {
                            "function": {
                                "name": ROLLOUT_TOOL.name,
                                "arguments": {**ROLLOUT_ARGS, "percentage": "ten percent"},
                            }
                        }
                    ],
                }
            },
        )
    )
    event = InboundEvent(
        channel="test",
        tenant=TENANT,
        user_id=USER,
        session_id=run.session_id,
        text="roll out exp-7",
    )

    result = handle(stack, event, scopes=SCOPES, run=run)

    assert result.status == "rejected"
    assert "tool.reject" in result.tracer.names()
    assert result.text, "a refused proposal must still leave the turn with an answer"
    assert stack.registry_client.rollouts == [], "a malformed proposal reached the surface"


def test_the_configured_host_reaches_the_client_factory() -> None:
    """The seam hands the host over; a factory that quietly connected elsewhere would
    make every other test here true of the wrong serving system."""
    seen: list[str] = []

    def build(host: str) -> FakeClient:
        seen.append(host)
        return FakeClient(host)

    OllamaEngine(host="http://elsewhere:11434", build_client=build).generate(request())

    assert seen == ["http://elsewhere:11434"]


def test_a_client_that_cannot_be_built_is_not_reported_as_a_bad_answer() -> None:
    """`ModelUnavailable` from the factory passes through rather than being wrapped in
    another `ModelUnavailable` about the model failing to answer. It never got asked."""

    def build(host: str) -> Any:
        raise ModelUnavailable(f"no client for {host}")

    with pytest.raises(ModelUnavailable, match="no client for"):
        OllamaEngine(build_client=build).generate(request())
