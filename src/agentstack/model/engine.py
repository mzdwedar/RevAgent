"""Engine adapters. A provider swap is an adapter change, not a runtime change."""

from __future__ import annotations

from typing import Protocol

from agentstack.model.contract import (
    ModelAsset,
    ModelRequest,
    ModelResponse,
    ToolCallProposal,
)


class ModelEngine(Protocol):
    """Converts a prepared request into output. Owns nothing around itself."""

    asset: ModelAsset

    def generate(self, request: ModelRequest) -> ModelResponse: ...


class EchoEngine:
    """A deterministic stand-in so the stack is runnable and testable without a provider.

    It proposes a tool call when the assembled context names one of the exposed tools.
    That is enough to exercise every layer above it; it is not a model.
    """

    def __init__(self, asset: ModelAsset | None = None) -> None:
        self.asset = asset or ModelAsset(name="echo-0", context_window=8192, max_output_tokens=512)

    def generate(self, request: ModelRequest) -> ModelResponse:
        arguments = {
            token.split("=", 1)[0]: token.split("=", 1)[1]
            for token in request.rendered_context.split()
            if "=" in token
        }
        proposals: tuple[ToolCallProposal, ...] = ()
        for tool in request.exposed_tools:
            if tool in request.rendered_context:
                proposals = (ToolCallProposal(tool=tool, arguments=arguments),)
                break
        return ModelResponse(text=f"echo: {request.rendered_context[:200]}", proposals=proposals)
