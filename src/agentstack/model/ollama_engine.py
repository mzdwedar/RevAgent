"""A model engine over a locally served Ollama model (Part 2).

Named `ollama_engine` and not `ollama`, because a module that imports a top-level
package of its own name is a trap for the first person who adds a relative import.

**Why this package may talk to a serving system.** `CONSTRAINTS.md` says only
`agentstack.execution` reaches the world, and `lint-imports` contract 3 forbids this
package an HTTP client of its own. Neither is bent here. Part 2 separates the model
*asset* from the *serving system* from the *interaction contract*: this module owns the
contract and uses the serving system's own client to reach it. The agent does not act
*on* Ollama any more than it acts on Postgres - it thinks with one and remembers in the
other, and both are substrate. An execution surface is a system the agent changes, and
those still go through the gateway with an envelope, an approval and containment.

Worth saying plainly: `ollama` is not in contract 3's forbidden list, so importing it
here would have passed silently either way. Satisfying the letter of a rule while
breaking its spirit is the thing this repository exists to catch, so the reasoning is
written down rather than left to the contract's silence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentstack.model.contract import (
    ModelAsset,
    ModelRequest,
    ModelResponse,
    ToolCallProposal,
)

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_HOST = "http://localhost:11434"

# Explicit, and recorded in SPEC.md's Foundation Assumptions. Ollama's own default is
# smaller than most people assume, and a context window discovered by watching answers
# get worse is the Part 2 confusion in miniature - a serving-system setting mistaken
# for a property of the model.
DEFAULT_NUM_CTX = 8192
DEFAULT_MAX_OUTPUT_TOKENS = 1024


class ModelUnavailable(RuntimeError):
    """The serving system could not be reached or could not answer."""


@dataclass(frozen=True, slots=True)
class OllamaEngine:
    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    num_ctx: int = DEFAULT_NUM_CTX
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    # Zero by default. Criterion 17 wants the same inputs to produce the same
    # experiment, and a drafting step that rephrases itself each run makes an
    # `experiment_version` that covers the model useless.
    temperature: float = 0.0
    # qwen3 is a reasoning model. Its thinking is not an answer, and leaving it on puts
    # a monologue in the text the turn returns to a human.
    think: bool = False
    # A seam for tests, so the adapter can be exercised without a model on the machine.
    build_client: Callable[[str], Any] | None = field(default=None)

    @property
    def asset(self) -> ModelAsset:
        """The weights, described separately from whoever serves them."""
        return ModelAsset(
            name=self.model,
            context_window=self.num_ctx,
            max_output_tokens=self.max_output_tokens,
        )

    def _client(self) -> Any:
        if self.build_client is not None:
            return self.build_client(self.host)
        # No ImportError guard: `ollama` is a declared dependency, not an optional
        # extra like `tabpfn`. Guarding an import that cannot fail is dead code that
        # reads as caution.
        from ollama import Client

        return Client(host=self.host)

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Ask the model, and return what it *proposed*.

        Everything coming back is untrusted. The tool name may not be one that was
        offered, the arguments may be the wrong shape, and both are someone else's
        problem by design: the registry validates against the schema and the exposure
        filter refuses a tool this run was never shown. This adapter's only job is to
        report faithfully what was said.
        """
        try:
            answer = self._client().chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": request.instructions},
                    {"role": "user", "content": request.rendered_context},
                ],
                tools=[_as_tool(tool) for tool in request.exposed_tools],
                think=self.think,
                stream=False,
                options={
                    "num_ctx": self.num_ctx,
                    "num_predict": request.max_output_tokens,
                    "temperature": self.temperature,
                },
            )
        except ModelUnavailable:
            raise
        except Exception as exc:
            raise ModelUnavailable(
                f"{self.model} at {self.host} could not answer: {exc}. The turn stops "
                "here rather than continuing without a model."
            ) from exc

        message = _field(answer, "message") or {}
        return ModelResponse(
            text=str(_field(message, "content") or ""),
            proposals=tuple(_proposals(_field(message, "tool_calls") or ())),
        )


def _as_tool(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _proposals(tool_calls: Any) -> list[ToolCallProposal]:
    proposals: list[ToolCallProposal] = []
    for call in tool_calls:
        function = _field(call, "function") or {}
        name = _field(function, "name")
        if not name:
            # A tool call with no name is not a proposal about anything. Dropping it
            # keeps the turn answerable; the model's text still reaches the user.
            continue
        arguments = _field(function, "arguments") or {}
        proposals.append(ToolCallProposal(tool=str(name), arguments=dict(arguments)))
    return proposals


def _field(source: Any, name: str) -> Any:
    """Read a field from either a mapping or an object.

    The ollama client returns pydantic models; a recorded fixture and a test fake
    return dicts. Handling both keeps the seam usable without making tests construct
    library types they have no reason to know about.
    """
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)
