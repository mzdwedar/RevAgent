"""Layer 4 - model engine and inference.

Three separate choices (Part 2, ADR-0003): the model asset, the serving system, and
the interaction contract. This package owns only the contract; it does not own the
runtime around it, and what it returns is a proposal, not an execution.
"""

from agentstack.model.contract import (
    ModelAsset,
    ModelRequest,
    ModelResponse,
    ToolCallProposal,
)
from agentstack.model.engine import EchoEngine, ModelEngine

__all__ = [
    "EchoEngine",
    "ModelAsset",
    "ModelEngine",
    "ModelRequest",
    "ModelResponse",
    "ToolCallProposal",
]
