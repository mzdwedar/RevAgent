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
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence


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
