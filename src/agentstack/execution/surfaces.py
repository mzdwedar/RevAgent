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


class SurfaceTimeout(RuntimeError):
    """The surface did not answer. Whether the effect applied is unknown."""


class SurfaceRefused(RuntimeError):
    """The surface answered, and the answer is that nothing applied.

    The opposite of `SurfaceTimeout`, and only a surface that can prove it may raise it:
    a guarded write that matched no row, a precondition checked in the same statement
    as the change. "The resource is not in the state this action needs" - a draft
    already discarded, an experiment no longer live - is this, not an error.

    The gateway releases the idempotency claim for it. Raising it for an effect that
    *might* have applied would turn an unknown outcome into a free slot, which is the
    double-refund the two-phase ledger exists to prevent.
    """


class SurfaceClient(Protocol):
    """A real thing you can ask about, and a real thing you can change.

    Two verbs, deliberately. One verb would force `commit` to branch GET-vs-POST off
    a resource string assembled from model-supplied arguments, and would leave a read
    with no way to return what it read - which is how tool functions start fetching
    things themselves.
    """

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]: ...

    def commit(self, resource: str, payload: dict[str, Any]) -> str: ...


@dataclass(slots=True)
class RecordingClient:
    """Reference client.

    Commits and reads are counted separately so a duplicated side effect is visible
    in tests and a cached read cannot hide behind the same number.
    """

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    reads: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_after_effect: bool = False
    # Refuse before acting: the precondition did not hold, and nothing applied.
    refuse_before_effect: bool = False

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        self.reads.append((resource, dict(query)))
        return {"resource": resource, "read_index": len(self.reads)}

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        if self.refuse_before_effect:
            raise SurfaceRefused(f"{resource}: refused, nothing applied")
        self.calls.append((resource, dict(payload)))
        if self.fail_after_effect:
            # The effect applied; the answer never came back. This is the failure the
            # whole idempotency design exists for, and a client that cannot produce it
            # makes every test about it vacuous.
            raise SurfaceTimeout(f"{resource}: applied, but the response was lost")
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


@dataclass(slots=True)
class RegistryClient:
    """The experiment registry, in process.

    A fake for iteration 1, and named as one. It keeps drafts and rollouts so a test can
    ask what actually reached the surface, which is the question every effect invariant
    in this repository is really asking.

    What it deliberately does not do is validate. A surface that rejected a malformed
    draft would make the schema validator look unnecessary, and the point of criterion
    19 is that nothing malformed gets this far.
    """

    drafts: dict[str, dict[str, Any]] = field(default_factory=dict)
    rollouts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    reads: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Apply the effect and then lose the answer: the failure two-phase idempotency
    # exists for. Same switch as RecordingClient, for the same reason.
    fail_after_effect: bool = False

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        self.reads.append((resource, dict(query)))
        return dict(self.drafts.get(resource, {}))

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        if resource.endswith("/rollout"):
            self.rollouts.append((resource, dict(payload)))
            receipt = f"rollout-{len(self.rollouts)}"
        else:
            self.drafts[resource] = dict(payload)
            receipt = f"draft-{len(self.drafts)}"
        if self.fail_after_effect:
            raise SurfaceTimeout(f"{resource} applied, answer lost")
        return receipt
