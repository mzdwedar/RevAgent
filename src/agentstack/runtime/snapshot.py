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

`resource_state` is the seam where the real thing arrives: the charge's status, the
subscription's version, whatever the domain says identifies "this resource, now".
Until SPEC.md answers that, the runtime supplies the effects it has already committed
against that resource this run, which is the part it can know on its own. Replacing
it is a change to this function and the call site - the approval semantics above do
not move.
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
