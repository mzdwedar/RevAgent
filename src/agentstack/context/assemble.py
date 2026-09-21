"""Assembling the bounded working set for one turn (Part 5).

`assemble` is a pure function of named inputs. That is the whole design: the prompt
context becomes a derived, inspectable, reproducible value that can be snapshot into
the trace and diffed at a failure point - rather than a side effect of whatever the
loop happened to append.

Compaction happens here, and it changes only what the model sees. It never touches the
transcript: the source material stays where the control plane put it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from agentstack.context.items import ContextItem, Scope, Trust
from agentstack.context.memory import MemoryRecord
from agentstack.context.retrieval import Candidate


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """A prepared view. The canonical record lives in the transcript store."""

    instructions: str
    items: tuple[ContextItem, ...]
    dropped_out_of_scope: int
    dropped_over_budget: int

    def render(self) -> str:
        lines = [self.instructions]
        for item in self.items:
            lines.append(f"[{item.kind}|{item.trust.value}|{item.provenance}] {item.text}")
        return "\n".join(lines)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.render().encode()).hexdigest()[:16]

    def untrusted(self) -> tuple[ContextItem, ...]:
        return tuple(i for i in self.items if i.trust is Trust.UNTRUSTED)


def assemble(
    *,
    instructions: str,
    latest_message: ContextItem,
    history: Sequence[ContextItem] = (),
    retrieved: Sequence[Candidate] = (),
    memories: Sequence[MemoryRecord] = (),
    requester_scope: Scope,
    budget_items: int = 20,
) -> ContextBundle:
    """Build the turn's working set.

    Every candidate is scope-checked against the run's own scope before it can enter.
    Anything the run is not entitled to see is dropped and counted, not silently
    omitted - the count is what makes a leak visible in the trace.
    """
    candidates: list[ContextItem] = [latest_message, *history]
    candidates += [c.as_context_item(reason="retrieved for this question") for c in retrieved]
    candidates += [m.as_context_item(reason="durable memory for this scope") for m in memories]

    in_scope: list[ContextItem] = []
    out_of_scope = 0
    for item in candidates:
        if requester_scope.covers(item.scope):
            in_scope.append(item)
        else:
            out_of_scope += 1

    kept = in_scope[:budget_items]
    return ContextBundle(
        instructions=instructions,
        items=tuple(kept),
        dropped_out_of_scope=out_of_scope,
        dropped_over_budget=len(in_scope) - len(kept),
    )
