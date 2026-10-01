"""The interaction contract (Model engine & inference).

Two things this module is careful about:

* A tool call that comes back is a `ToolCallProposal`. The model shapes arguments;
  application code decides whether to execute, refuse or validate.
* Structured output is untrusted input. Schema-valid JSON solves packaging, not
  correctness, authorization or policy compliance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelAsset:
    """The weights, separate from whoever serves them (Model engine & inference)."""

    name: str
    context_window: int
    max_output_tokens: int
    modalities: tuple[str, ...] = ("text",)


@dataclass(frozen=True, slots=True)
class ToolCallProposal:
    """What the model asked for. Not what happens."""

    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExposedTool:
    """One tool as the model is shown it: a name, a description, a parameter schema.

    Plain data, not a `ToolSpec`. Layer 6 sits above layer 4, so the model package
    cannot import the registry - and it should not want to. What a model needs is the
    menu; what a `ToolSpec` carries beyond that is the scope, the surface, the approval
    tier and the idempotency policy, none of which the model gets a say in. Handing it
    the whole spec would be handing it the authority metadata to reason about.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelRequest:
    instructions: str
    rendered_context: str
    exposed_tools: tuple[ExposedTool, ...]
    max_output_tokens: int

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.exposed_tools)


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    proposals: tuple[ToolCallProposal, ...] = field(default=())
