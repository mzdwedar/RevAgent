"""The interaction contract (Part 2).

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
    """The weights, separate from whoever serves them (Part 2)."""

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
class ModelRequest:
    instructions: str
    rendered_context: str
    exposed_tools: tuple[str, ...]
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    proposals: tuple[ToolCallProposal, ...] = field(default=())
