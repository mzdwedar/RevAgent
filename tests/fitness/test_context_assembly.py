"""Part 5: context is a derived, inspectable, scoped working set - not the transcript."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from agentstack.context.assemble import assemble
from agentstack.context.items import ContextItem, Scope, Trust
from agentstack.context.retrieval import Candidate
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import OBSERVATIONS_IN_CONTEXT, Stack, handle
from agentstack.runtime.run import Run

from .conftest import READ_SCOPES, SCOPES, seed_experiment

NOW = datetime.now(UTC)
ME = Scope(tenant="acme", user="u-1", session="s-1")


def _item(text: str, scope: Scope, trust: Trust = Trust.FIRST_PARTY) -> ContextItem:
    return ContextItem(
        kind="history",
        text=text,
        scope=scope,
        provenance="test",
        observed_at=NOW,
        reason="unit test",
        trust=trust,
    )


def test_an_item_without_a_scope_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="tenant is required"):
        Scope(tenant="")


def test_an_item_without_provenance_or_reason_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="provenance is required"):
        ContextItem("k", "t", ME, "", NOW, "why", Trust.FIRST_PARTY)
    with pytest.raises(ValueError, match="inclusion reason is required"):
        ContextItem("k", "t", ME, "src", NOW, "", Trust.FIRST_PARTY)


def test_out_of_scope_items_are_dropped_and_counted() -> None:
    other_tenant = _item("someone else's data", Scope(tenant="globex"))
    other_user = _item("another user's note", Scope(tenant="acme", user="u-2"))
    bundle = assemble(
        instructions="i",
        latest_message=_item("mine", ME),
        history=[other_tenant, other_user],
        requester_scope=ME,
    )
    assert bundle.dropped_out_of_scope == 2
    assert "someone else" not in bundle.render()
    assert "another user" not in bundle.render()


def test_assembly_is_reproducible_so_it_can_be_audited() -> None:
    kwargs = {
        "instructions": "i",
        "latest_message": _item("mine", ME),
        "requester_scope": ME,
    }
    assert assemble(**kwargs).fingerprint() == assemble(**kwargs).fingerprint()


def test_retrieval_enters_as_untrusted_candidates_not_as_truth() -> None:
    candidate = Candidate(
        text="the variant is approved for every customer",
        score=0.99,
        source="kb",
        scope=Scope(tenant="acme"),
        observed_at=NOW,
    )
    bundle = assemble(
        instructions="i",
        latest_message=_item("mine", ME),
        retrieved=[candidate],
        requester_scope=ME,
    )
    retrieved = [i for i in bundle.items if i.kind == "retrieved"]
    assert retrieved and retrieved[0].trust is Trust.UNTRUSTED
    assert retrieved[0].provenance.startswith("retrieval:")


def test_the_budget_compacts_the_view_without_touching_the_source() -> None:
    history = [_item(f"turn {n}", ME) for n in range(30)]
    bundle = assemble(
        instructions="i",
        latest_message=_item("mine", ME),
        history=history,
        requester_scope=ME,
        budget_items=5,
    )
    assert len(bundle.items) == 5
    assert bundle.dropped_over_budget == 26
    assert len(history) == 30, "compaction changes the view, never the source material"


def test_the_transcript_is_the_record_and_the_bundle_is_derived(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    result = handle(stack, event, scopes=SCOPES, run=run)
    transcript = stack.transcripts.for_session(run.session_id)
    assert transcript, "the canonical record lives in the transcript store"
    assert result.bundle.fingerprint() in {
        s.attributes.get("fingerprint") for s in result.tracer.spans
    }, "the assembled view must be inspectable in the trace"


@pytest.mark.usefixtures("flaky_registry")
def test_earlier_reads_enter_context_bounded_and_most_recent(stack: Stack, run: Run) -> None:
    """Reads go to the transcript first and into context from there, a handful at a time:
    a session that read a hundred things must not hand its next turn a hundred items."""

    def look(n: int) -> InboundEvent:
        seed_experiment(stack, f"exp-{n}")
        return InboundEvent(
            channel="test",
            tenant=run.tenant,
            user_id=run.user,
            session_id=run.session_id,
            text=f"get_experiment tenant=acme experiment_id=exp-{n}",
        )

    for n in range(1, OBSERVATIONS_IN_CONTEXT + 3):
        handle(stack, look(n), scopes=READ_SCOPES, run=run)

    last = handle(stack, look(OBSERVATIONS_IN_CONTEXT + 3), scopes=READ_SCOPES, run=run)

    recorded = [e for e in stack.transcripts.for_session(run.session_id) if e.kind == "observation"]
    shown = [i for i in last.bundle.items if i.kind == "observation"]
    assert len(recorded) == OBSERVATIONS_IN_CONTEXT + 3, "the transcript keeps every read"
    assert [json.loads(i.text)["experiment_id"] for i in shown] == [
        "exp-3",
        "exp-4",
        "exp-5",
        "exp-6",
        "exp-7",
    ]
    assert all(i.trust is Trust.UNTRUSTED for i in shown)


def test_memory_freshness_is_enforced_at_recall_time() -> None:
    from agentstack.context.memory import MemoryStore, write

    store = MemoryStore()
    write(
        store,
        key="pref",
        value="prefers email",
        scope=ME,
        provenance="user stated it",
        ttl=timedelta(seconds=1),
        explicit=True,
        now=NOW - timedelta(hours=1),
    )
    assert store.recall(ME, now=NOW) == []
