"""Tool declarations - shape, identity, scope, surface, reversibility, approval.

The schema half of a tool answers "is this well-formed". Everything else in `ToolSpec`
exists because the schema cannot answer the questions that actually matter: who acts,
against which resources, where it runs, whether it can be undone, and whether a human
has to say yes first (Tools, MCP, capability surfaces).

Prefer narrow, intention-specific tools. `create_customer_reply_draft` has a blast
radius you can reason about; `send_email` does not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

REQUIRED_TOOL_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "input_schema",
        "acts_as",
        "scope",
        "surface",
        "side_effecting",
        "reversible",
        "approval",
        "idempotency",
        "stages",
    }
)


class Surface(Enum):
    """Where an action would actually occur. Named, never reasoned about in the abstract."""

    NONE = "none"
    API = "api"
    # The experiment registry: where candidates are written and rollouts recorded.
    REGISTRY = "registry"
    DATABASE = "database"
    BROWSER = "browser"
    SHELL = "shell"
    FILESYSTEM = "filesystem"
    MAIL = "mail"


class ActsAs(Enum):
    USER = "user"
    DELEGATED = "delegated"
    SERVICE = "service"


class Approval(Enum):
    NONE = "none"
    PRE_COMMIT = "pre_commit"
    ALWAYS = "always"


class Idempotency(Enum):
    NONE = "none"
    KEY = "key"
    NATURAL = "natural"


@dataclass(frozen=True, slots=True)
class Fixed:
    """A payload value the tool always writes, and no request under it may vary.

    `because` is what the refusal says. A halt whose payload names 5% is not a halt
    with a typo, it is a rollout wearing a halt's name, and the audit record should
    say so in those words rather than as a key/value diff.
    """

    value: Any
    because: str


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    acts_as: ActsAs
    scope: str
    surface: Surface
    side_effecting: bool
    reversible: bool
    approval: Approval
    idempotency: Idempotency
    reversal_note: str = ""
    stages: frozenset[str] = field(default_factory=lambda: frozenset({"default"}))
    tenants: frozenset[str] | None = None
    # What binds a request to this spec, beyond its name and surface (both always
    # checked). `verb` is the action the surface serves this tool's resource as - the
    # registry derives it from the resource's suffix, so a resource that ends in
    # `/rollout` *is* a rollout whatever tool prepared it. Declared here so the gateway
    # can refuse a request whose resource names a different act than the tool that was
    # authorised (H2), before policy or approval look at it.
    verb: str | None = None
    # Payload values this tool's prepare always writes. The gateway refuses a request
    # that varies one, whatever was approved: `halt_rollout` fixes `percentage` at 0.
    fixed_payload: Mapping[str, Fixed] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.description:
            raise ValueError(f"{self.name}: a description is part of the capability surface")
        if "type" not in self.input_schema:
            raise ValueError(f"{self.name}: input_schema must declare a type")
        if not self.scope:
            raise ValueError(
                f"{self.name}: a resource scope is required (Tools, MCP, capability surfaces)"
            )
        if not self.stages:
            raise ValueError(f"{self.name}: declare the stages this tool is exposed in")
        if self.surface is Surface.REGISTRY and not self.verb:
            raise ValueError(
                f"{self.name}: a registry tool declares the verb its resource names - the "
                "surface takes the act from the resource, so the spec has to say which act"
            )
        if self.side_effecting:
            if self.idempotency is Idempotency.NONE:
                raise ValueError(
                    f"{self.name}: a side-effecting tool needs an idempotency policy - "
                    "retry without it duplicates the effect "
                    "(Runtime, workflows, durable execution)"
                )
            if self.approval is Approval.NONE:
                raise ValueError(
                    f"{self.name}: a side-effecting tool needs an approval tier "
                    "(Identity, trust, policy, approvals)"
                )
            if not self.reversible:
                if self.approval is not Approval.ALWAYS:
                    raise ValueError(
                        f"{self.name}: irreversible tools require approval=ALWAYS "
                        "(Identity, trust, policy, approvals)"
                    )
                if not self.reversal_note.strip():
                    raise ValueError(
                        f"{self.name}: an irreversible tool needs a reversal note - "
                        "'irreversible' lands with an approver only when it says what "
                        "undoing would actually take (Identity, trust, policy, approvals)"
                    )
        elif self.surface in {Surface.SHELL, Surface.FILESYSTEM}:
            raise ValueError(f"{self.name}: {self.surface.value} is never side-effect free")

    def requires_approval(self) -> bool:
        return self.approval is not Approval.NONE
