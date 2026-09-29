"""The state an approval is bound to (Parts 7 and 8).

An approval is only meaningful against a described world. The first version of this
used the prompt's fingerprint for the whole turn, which got two things wrong: every
proposal in a turn shared one snapshot, so the second was approved against a world in
which the first had not yet committed; and the prompt fingerprint is not the world.

What a snapshot has to cover is the state of **the resource this action changes**:

* per resource, so committing one charge does not invalidate an approval already
  given for a different charge, and does invalidate one for the same charge;
* per proposal, so the order of effects within a turn is visible to staleness;
* including the rendered context, so an approval does not survive the request that
  produced it being replaced.

`resource_state` is the seam where the real thing arrives: whatever the domain says
identifies "this resource, now". A surface that can describe its resource says so
(`SurfaceClient.state`, read through `Gateway.observe`), and then the snapshot is that
description and nothing else: `world_snapshot`. It can be read again at the act, by a
process that never saw the turn (T43, ADR-0008 rule 3), and an approval against a
world that has since moved is stale. The experiment registry is the first such surface:
its experiment's version, variant and hypothesis.

A surface that can't describe its resource falls back to what the run itself has
committed against it, plus the rendered context: the part the runtime can know on its
own.

**Only an `ALWAYS` action is bound to the world** (`binds_the_world`). That tier is the
one whose approval is carried across time: a person answers hours after the turn
parked, and the commit reads the world again to find out whether what they approved
still exists (ADR-0008 rule 3). A `PRE_COMMIT` grant is minted by a rule at the act
itself, so there is nothing for the world to have moved away from, and binding it to
the world costs exactly one thing: an effect that moves its own description (a draft
*creates* the experiment the registry then describes) makes the rerun that should
deduplicate read its own grant as stale, and park a question nobody is asked (H1). The
catalog-wide statement of that is `tests/fitness/test_effects_keep_their_snapshot.py`:
no tool's own effect moves the snapshot its approval is checked against.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import Approval, ToolSpec


def resource_snapshot(
    context_fingerprint: str,
    resource: str,
    resource_state: Sequence[str],
) -> str:
    """Identify the world this action is about to change."""
    material = "|".join([context_fingerprint, resource, *resource_state])
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def world_snapshot(resource: str, world: Sequence[str]) -> str:
    """Identify the resource as its surface describes it now.

    No context fingerprint: anything that reads the world again, at the act, gets the
    same snapshot unless the world moved. That is what makes staleness checkable by the
    commit rather than carried to it.
    """
    return resource_snapshot("world", resource, world)


def binds_the_world(spec: ToolSpec) -> bool:
    """Whether this action's approval is checked against the world its surface describes.

    `ALWAYS` only: the tier a person answers, later, about a world the commit has to
    read again. Anything else is granted (or needs nothing) at the act, against what the
    run itself knows.
    """
    return spec.approval is Approval.ALWAYS


def approval_snapshot(
    *,
    spec: ToolSpec,
    request: ActionRequest,
    observe: Callable[[], Sequence[str] | None],
    context_fingerprint: str,
    committed: Sequence[str],
) -> str:
    """The state this action's approval binds to, as the turn computes it.

    `observe` is only called for an action bound to the world (a read through the
    gateway, so only then is the surface asked). When the surface can't describe the
    resource, or the action isn't bound to the world, the snapshot is what this turn
    knows: its context and what the run already committed against the resource.
    """
    world = observe() if binds_the_world(spec) else None
    if world is not None:
        return world_snapshot(request.resource, world)
    return resource_snapshot(context_fingerprint, request.resource, committed)
