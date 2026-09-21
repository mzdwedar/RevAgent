"""Named execution surfaces, and the containment around them (Part 7).

Do not reason about execution in the abstract. The same `call_api` label over a public
weather endpoint and over a production database are different risk classes, and only
the surface name distinguishes them.

**This is the only package permitted to import a real client library** - HTTP, DB,
shell, browser, mailer. `.importlinter` contract 3 enforces that; if you find yourself
adding `import httpx` anywhere else, the layer boundary is the thing that is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agentstack.tools.spec import Surface


class SandboxViolation(PermissionError):
    """The action was permitted, but it reached outside its containment."""


class SurfaceClient(Protocol):
    """A real thing that changes: a mail system, a database, a browser session."""

    def commit(self, resource: str, payload: dict[str, Any]) -> str: ...


@dataclass(slots=True)
class RecordingClient:
    """Reference client. Counts commits so duplicate side effects are visible in tests."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        self.calls.append((resource, dict(payload)))
        return f"receipt-{len(self.calls)}"


@dataclass(frozen=True, slots=True)
class Sandbox:
    """Blast-radius limits, declared per dimension.

    Containment with no limit is theater - but so is a limit nothing reads. Every
    field here is enforced by `check()`, and `tests/fitness/test_containment.py`
    fails if a field is ever added without being enforced.

    Egress and filesystem containment are deliberately absent rather than declared
    and ignored: there is no real client to bound yet, and a configured allowlist
    that nothing consults stops the next reviewer from asking. They land with the
    first real surface client (ADR-0004).
    """

    tenant: str
    allowed_surfaces: frozenset[Surface]
    allowed_resource_prefixes: frozenset[str]

    def check(self, *, surface: Surface, resource: str) -> None:
        if surface not in self.allowed_surfaces:
            raise SandboxViolation(f"{surface.value} is outside this run's containment")
        if not any(resource.startswith(p) for p in self.allowed_resource_prefixes):
            raise SandboxViolation(f"{resource} is outside this run's allowed resources")
        if not resource.startswith(f"{self.tenant}/"):
            raise SandboxViolation(f"{resource} crosses the tenant boundary")
