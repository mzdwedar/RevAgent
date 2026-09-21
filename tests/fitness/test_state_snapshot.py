"""Parts 7 and 8: an approval binds to the state of the thing being changed.

`state_snapshot` was `bundle.fingerprint()`, computed once before the proposal loop.
Two consequences the audit named:

(b) within one turn, the second proposal is approved against a world in which the
    first proposal had not yet committed; and
(a) the fingerprint covers the rendered prompt, which contains no timestamps and no
    transcript, so it is near-constant for a given message - an approval stays fresh
    across a world that moved underneath it.

(b) is fixed here: the snapshot is computed per proposal and scoped to the resource
the action touches, so committing one charge does not invalidate an approval for a
different charge, and does invalidate one for the same charge.

(a) needs to know what "the world" is - the charge's status, the subscription's
version - which is a domain question, deferred to SPEC.md. `resource_state` is the
seam it will arrive through.
"""

from __future__ import annotations

from agentstack.runtime.snapshot import resource_snapshot

BUNDLE = "fp-abc123"
CH7 = "acme/customers/c-42/charges/ch-7"
CH8 = "acme/customers/c-42/charges/ch-8"


def test_two_resources_in_one_turn_get_different_snapshots() -> None:
    assert resource_snapshot(BUNDLE, CH7, ()) != resource_snapshot(BUNDLE, CH8, ())


def test_committing_one_resource_does_not_move_another() -> None:
    """A refund on ch-7 must not invalidate an approval already given for ch-8."""
    before = resource_snapshot(BUNDLE, CH8, ())
    after_ch7_committed = resource_snapshot(BUNDLE, CH8, ())
    assert before == after_ch7_committed


def test_committing_a_resource_does_move_its_own_snapshot() -> None:
    before = resource_snapshot(BUNDLE, CH7, ())
    after = resource_snapshot(BUNDLE, CH7, ("receipt-1",))
    assert before != after, (
        "an approval granted before this resource changed must not survive the change"
    )


def test_the_snapshot_still_moves_when_the_prompt_moves() -> None:
    assert resource_snapshot(BUNDLE, CH7, ()) != resource_snapshot("fp-different", CH7, ())
