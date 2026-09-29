"""This project's capability catalog: the experiment registry's tools, and nothing else.

Every tool here prepares an `ActionRequest` against the registry surface; none performs
I/O. The gateway commits it. The tools sit on different stages (draft, evaluation,
rollout), so no run is ever shown more than its own stage's menu - a drafting turn cannot
see the rollout tool.
"""

from __future__ import annotations

from agentstack.tools.experiments import (
    ABSTAIN,
    DISCARD,
    DRAFT,
    GET,
    HALT,
    HISTORY,
    LIST,
    REVISE,
    ROLLOUT,
    prepare_abstain,
    prepare_discard,
    prepare_draft,
    prepare_get,
    prepare_halt,
    prepare_history,
    prepare_list,
    prepare_revise,
    prepare_rollout,
)
from agentstack.tools.registry import Registry


def build_registry() -> Registry:
    """Every tool this system has, on the stages they belong to."""
    registry = Registry()
    registry.register(DRAFT, prepare_draft)
    registry.register(ROLLOUT, prepare_rollout)
    registry.register(GET, prepare_get)
    registry.register(LIST, prepare_list)
    registry.register(HISTORY, prepare_history)
    registry.register(REVISE, prepare_revise)
    registry.register(DISCARD, prepare_discard)
    registry.register(ABSTAIN, prepare_abstain)
    registry.register(HALT, prepare_halt)
    return registry
