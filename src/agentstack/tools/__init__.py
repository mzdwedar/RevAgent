"""Layer 6 - tools, MCP, capability surfaces.

A tool definition is a capability surface, not a permission. It says what may be
*requested*, under which identity, against which scope, on which execution surface,
and what it would take to approve. Whether it happens is decided one layer up.

Nothing in this package performs I/O. A tool prepares an `ActionRequest`; only
`agentstack.execution.gateway` commits one (Tools, MCP, capability surfaces -> Execution surfaces).
"""

from agentstack.tools.action import ActionRequest
from agentstack.tools.registry import Registry, ToolNotExposed
from agentstack.tools.spec import (
    REQUIRED_TOOL_FIELDS,
    ActsAs,
    Approval,
    Fixed,
    Idempotency,
    Surface,
    ToolSpec,
)
from agentstack.tools.validation import InvalidToolArguments, validate_arguments

__all__ = [
    "REQUIRED_TOOL_FIELDS",
    "ActionRequest",
    "ActsAs",
    "Approval",
    "Fixed",
    "Idempotency",
    "InvalidToolArguments",
    "Registry",
    "Surface",
    "ToolNotExposed",
    "ToolSpec",
    "validate_arguments",
]
