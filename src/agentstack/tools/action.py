"""The request a tool prepares. Preparation is not commitment (Part 7)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agentstack.tools.spec import Surface


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """Fully-formed intent, not yet executed.

    Drafting an email, filling a form, generating SQL and staging a deletion all
    produce one of these. Sending, submitting, executing and deleting happen only in
    `agentstack.execution.gateway`.
    """

    tool: str
    surface: Surface
    resource: str
    payload: dict[str, Any]
    idempotency_key: str

    def fingerprint(self) -> str:
        """What an approval is bound to. Change any of it and the approval is void."""
        blob = json.dumps(
            {
                "tool": self.tool,
                "surface": self.surface.value,
                "resource": self.resource,
                "payload": self.payload,
            },
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]
