"""The registry, and the per-run exposure filter that keeps blast radius small.

Exposing every tool on every run is the cheapest way to hand a prompt injection a
menu. `expose_for` narrows the menu to the tools this tenant, at this stage of this
workflow, is meant to be able to request (Part 6).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import ToolSpec

Prepare = Callable[[Mapping[str, Any]], ActionRequest]


class ToolNotExposed(RuntimeError):
    """Raised when a run asks for a tool that was not exposed to it."""


@dataclass(slots=True)
class Registry:
    _specs: dict[str, ToolSpec] = field(default_factory=dict)
    _prepare: dict[str, Prepare] = field(default_factory=dict)

    def register(self, spec: ToolSpec, prepare: Prepare) -> None:
        if spec.name in self._specs:
            raise ValueError(f"{spec.name}: already registered")
        self._specs[spec.name] = spec
        self._prepare[spec.name] = prepare

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def spec(self, name: str) -> ToolSpec:
        return self._specs[name]

    def expose_for(self, *, tenant: str, stage: str = "default") -> tuple[ToolSpec, ...]:
        return tuple(
            spec
            for spec in self._specs.values()
            if stage in spec.stages and (spec.tenants is None or tenant in spec.tenants)
        )

    def prepare(
        self, name: str, arguments: Mapping[str, Any], *, exposed: tuple[ToolSpec, ...]
    ) -> ActionRequest:
        """Turn a model proposal into a concrete request - still without executing it."""
        if name not in {spec.name for spec in exposed}:
            raise ToolNotExposed(
                f"{name} was not exposed to this run; a proposal is not an entitlement"
            )
        return self._prepare[name](arguments)
