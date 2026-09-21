"""Layer 8 - identity, trust, policy, approvals.

Four separate controls that are routinely collapsed into one (Part 7):

* **Policy** decides whether an action is permitted at all.
* **Approval** decides whether this specific action should happen now.
* **Enforcement** applies both - and lives in the execution gateway, where the model
  cannot route around it.
* **Isolation** limits what the action can do once it proceeds. Approval is a decision
  point; isolation is a blast-radius limit. You need both.
"""

from agentstack.policy.approval import (
    ApprovalRecord,
    ApprovalRequired,
    ApprovalStale,
    ApprovalStore,
    require_approval,
)
from agentstack.policy.decisions import Decision, PolicyDenied, decide
from agentstack.policy.envelope import REQUIRED_ENVELOPE_FIELDS, IdentityEnvelope

__all__ = [
    "REQUIRED_ENVELOPE_FIELDS",
    "ApprovalRecord",
    "ApprovalRequired",
    "ApprovalStale",
    "ApprovalStore",
    "Decision",
    "IdentityEnvelope",
    "PolicyDenied",
    "decide",
    "require_approval",
]
