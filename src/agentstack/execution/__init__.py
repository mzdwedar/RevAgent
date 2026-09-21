"""Layer 7 - execution surfaces.

Capability exposure is not execution authority. This is the only package that may
touch a real client, and `Gateway.execute` is the only function in it that commits
anything. Everything upstream prepares; this is where the world changes.
"""

from agentstack.execution.gateway import (
    ExecutionResult,
    Gateway,
    ReadResult,
    UnresolvedEffect,
)
from agentstack.execution.idempotency import Claim, ClaimState, IdempotencyLedger
from agentstack.execution.surfaces import (
    RecordingClient,
    Sandbox,
    SandboxViolation,
    SurfaceClient,
    SurfaceTimeout,
)

__all__ = [
    "Claim",
    "ClaimState",
    "ExecutionResult",
    "Gateway",
    "IdempotencyLedger",
    "ReadResult",
    "RecordingClient",
    "Sandbox",
    "SandboxViolation",
    "SurfaceClient",
    "SurfaceTimeout",
    "UnresolvedEffect",
]
