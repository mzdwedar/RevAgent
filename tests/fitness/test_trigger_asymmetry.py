"""Looking is not deciding, and one trigger evaluates once (criteria 3 and 16).

The asymmetry is the load-bearing decision in the spec: waking on `metric_movement`
means evaluating precisely when noise is largest, so that wake may stop an experiment
and may never advance one. Without it the system is an optional-stopping machine -
structurally biased toward acting on the looks that flatter the variant.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from agentstack.interfaces.triggers import MalformedTrigger, parse_trigger
from agentstack.policy.triggers import (
    AUTHORITY,
    Outcome,
    OutcomeNotAuthorized,
    TriggerEvent,
    TriggerKind,
    authorize,
)
from agentstack.runtime.cycles import CycleStore, evaluate
from agentstack.storage.database import Database, IntegrityViolation

PAYLOAD = {
    "kind": "data_arrival",
    "experiment_id": "exp-7",
    "data_as_of": "telecom-bigml:f107d488f7bf4651",
    "tenant": "acme",
}


def trigger(**overrides: Any) -> TriggerEvent:
    return parse_trigger({**PAYLOAD, **overrides}, source="test")


# --- the ingress decides nothing ---


def test_a_well_formed_trigger_parses() -> None:
    event = trigger()

    assert event.kind is TriggerKind.DATA_ARRIVAL
    assert event.experiment_id == "exp-7"
    assert event.data_as_of == "telecom-bigml:f107d488f7bf4651"
    assert event.source == "test"


def test_an_unknown_kind_is_refused_rather_than_defaulted() -> None:
    """A default would hand a typo whichever authority the default carries."""
    with pytest.raises(MalformedTrigger, match="not a trigger kind"):
        trigger(kind="metric_movment")


@pytest.mark.parametrize("field", ["kind", "experiment_id", "data_as_of", "tenant"])
def test_every_field_is_required(field: str) -> None:
    payload = {**PAYLOAD}
    payload.pop(field)

    with pytest.raises((MalformedTrigger, ValueError)):
        parse_trigger(payload)


def test_a_trigger_without_a_watermark_cannot_be_acted_on() -> None:
    """`data_as_of` is what makes the cycle idempotent and what an approval is later
    bound against. A trigger without one has nothing to be about."""
    with pytest.raises(MalformedTrigger, match="data_as_of"):
        trigger(data_as_of="   ")


def test_an_identifier_field_is_not_a_payload() -> None:
    with pytest.raises(MalformedTrigger, match="identifier, not a payload"):
        trigger(experiment_id="x" * 5_000)


def test_the_ingress_does_not_resolve_identity_or_policy() -> None:
    """Interfaces & channels: the tenant on a trigger is a claim. Layer 8 tests it."""
    import inspect

    from agentstack.interfaces import triggers

    source = inspect.getsource(triggers)

    assert "authorize" not in source
    assert "Outcome" not in source


# --- the asymmetry ---


def test_a_metric_movement_may_stop_an_experiment() -> None:
    assert authorize(TriggerKind.METRIC_MOVEMENT, Outcome.ABSTAIN) is Outcome.ABSTAIN
    assert authorize(TriggerKind.METRIC_MOVEMENT, Outcome.CONTINUE) is Outcome.CONTINUE


def test_a_metric_movement_may_never_advance_one() -> None:
    with pytest.raises(OutcomeNotAuthorized, match="optional-stopping machine"):
        authorize(TriggerKind.METRIC_MOVEMENT, Outcome.PROPOSE)


def test_a_data_arrival_may_reach_every_outcome() -> None:
    """A watermark advancing is independent of what the data says, so this wake
    carries no bias to protect against."""
    assert AUTHORITY[TriggerKind.DATA_ARRIVAL] == frozenset(Outcome)


def test_the_asymmetry_is_data_not_branches() -> None:
    """Written so it can be read rather than traced. It is the design."""
    assert Outcome.PROPOSE not in AUTHORITY[TriggerKind.METRIC_MOVEMENT]
    assert Outcome.ABSTAIN in AUTHORITY[TriggerKind.METRIC_MOVEMENT]


def test_an_evaluator_that_proposes_on_a_metric_movement_is_refused(
    app_database: Database,
) -> None:
    """Enforced where the outcome is recorded, because the evaluator is the thing that
    might be wrong. A rule enforced only inside the code it constrains is a comment."""
    store = CycleStore(db=app_database)
    event = trigger(kind="metric_movement")

    with pytest.raises(OutcomeNotAuthorized):
        evaluate(store, event, lambda _: (Outcome.PROPOSE, "run-1"))

    parked = store.get(event)
    assert parked is not None
    assert parked.outcome is None, "a refused outcome was recorded as something else"


def test_the_database_refuses_it_too(app_database: Database) -> None:
    """Held twice. A `metric_movement` row recording a propose would be an
    optional-stopping machine with evidence."""
    app_database.execute(
        "INSERT INTO trigger_cycles (experiment_id, data_as_of, kind) VALUES (%s, %s, %s)",
        ("exp-db", "w-1", "metric_movement"),
    )

    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(
            "UPDATE trigger_cycles SET outcome = 'propose', settled_at = now()"
            " WHERE experiment_id = %s",
            ("exp-db",),
        )

    assert caught.value.constraint == "metric_movement_never_proposes"


# --- criterion 3: one evaluation per trigger ---


def test_a_trigger_delivered_twice_evaluates_once(app_database: Database) -> None:
    """At-least-once delivery meets an idempotent cycle. The second delivery must not
    score the cohort again, draft again, and ask a human again about a decision they
    already made."""
    store = CycleStore(db=app_database)
    event = trigger()
    runs: list[str] = []

    def evaluator(_: TriggerEvent) -> tuple[Outcome, str | None]:
        runs.append("evaluated")
        return Outcome.PROPOSE, "run-1"

    first = evaluate(store, event, evaluator)
    second = evaluate(store, event, evaluator)

    assert runs == ["evaluated"]
    assert first.outcome is Outcome.PROPOSE
    assert second.outcome is Outcome.PROPOSE
    assert second.claimed_at == first.claimed_at


def test_a_different_watermark_is_a_different_cycle(app_database: Database) -> None:
    store = CycleStore(db=app_database)
    runs: list[str] = []

    def evaluator(_: TriggerEvent) -> tuple[Outcome, str | None]:
        runs.append("evaluated")
        return Outcome.CONTINUE, None

    evaluate(store, trigger(), evaluator)
    evaluate(store, trigger(data_as_of="telecom-bigml:moved"), evaluator)

    assert len(runs) == 2


def test_a_data_arrival_is_not_swallowed_by_an_earlier_metric_movement(
    app_database: Database,
) -> None:
    """Why the key carries the kind as well as the watermark.

    Keyed on watermark alone, a `data_arrival` arriving after a `metric_movement` at the
    same watermark would be deduplicated away - and the trigger that may propose would
    be silently suppressed by the one that may not. That is an authority downgrade
    arriving disguised as a deduplication.
    """
    store = CycleStore(db=app_database)
    reached: list[Outcome] = []

    def evaluator(event: TriggerEvent) -> tuple[Outcome, str | None]:
        outcome = Outcome.PROPOSE if event.kind is TriggerKind.DATA_ARRIVAL else Outcome.CONTINUE
        reached.append(outcome)
        return outcome, None

    evaluate(store, trigger(kind="metric_movement"), evaluator)
    evaluate(store, trigger(kind="data_arrival"), evaluator)

    assert reached == [Outcome.CONTINUE, Outcome.PROPOSE]


def test_two_deliveries_arriving_together_produce_one_evaluation(
    app_database: Database,
) -> None:
    """Read-then-insert lets both deliveries see an empty table and both proceed."""
    store = CycleStore(db=app_database)
    event = trigger()
    runs: list[str] = []
    barrier = threading.Barrier(4)

    def deliver() -> None:
        barrier.wait()
        evaluate(store, event, lambda _: (runs.append("x"), (Outcome.CONTINUE, None))[1])

    threads = [threading.Thread(target=deliver) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(runs) == 1, f"{len(runs)} deliveries evaluated the same trigger"


def test_a_cycle_that_never_finished_is_visible(app_database: Database) -> None:
    """The same shape as an unresolved idempotency claim: an evaluation that stopped
    halfway is not a "no", and treating it as one silently skips a watermark."""
    store = CycleStore(db=app_database)
    event = trigger(kind="metric_movement")

    with pytest.raises(OutcomeNotAuthorized):
        evaluate(store, event, lambda _: (Outcome.PROPOSE, None))

    unsettled = store.unsettled()

    assert [c.experiment_id for c in unsettled] == ["exp-7"]
    assert unsettled[0].settled is False
