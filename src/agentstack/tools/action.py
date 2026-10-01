"""The request a tool prepares. Preparation is not commitment (Tools, MCP, capability surfaces)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agentstack.tools.spec import Surface


def idempotency_key(verb: str, *parts: object) -> str:
    """The key that names one effect, joined so that it names only that one.

    `/` is the separator because no id can contain it - it is what keeps an id to one
    segment of a resource path (C1). `:` would not do: it is a legal id character, and
    `a` + `b:c` joined with it is `a:b` + `c`, which let one experiment's halt be
    answered with another's receipt. Each tool writes its parts in a fixed order and
    count, so with a separator no part contains, the key is injective.

    Validation is what keeps `/` out of a part, not this function: a request built past
    validation must still reach the gateway, whose binding refuses it for its verb.
    `tests/fitness/test_idempotency.py` holds the injectivity over the whole catalog.
    """
    return "/".join([verb, *(str(part) for part in parts)])


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
