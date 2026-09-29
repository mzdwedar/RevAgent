"""The experiment registry's schema holds its invariants without trusting a caller (T23).

The client in `execution/surfaces.py` will write this store through guarded single
statements. These tests go around it on purpose: a history that is only append-only
because the one client happens to append is a convention, and the next client - or an
operator with psql - would not know it.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.execution.surfaces import (
    PostgresRegistryClient,
    RegistryClient,
    SurfaceClient,
    SurfaceRefused,
)
from agentstack.storage.database import Database, IntegrityViolation

TENANT = "acme"
EXPERIMENT = "exp-7"
VERSION = "exp:abc123"
SEED_HYPOTHESIS = "a discount retains at-risk customers"


def seed_draft(db: Database, *, experiment: str = EXPERIMENT, version: str = VERSION) -> None:
    """A draft as `create_experiment_draft` will write it: three rows, one statement."""
    db.execute(
        "WITH e AS ("
        "  INSERT INTO experiments (tenant, experiment_id, status, current_version)"
        "  VALUES (%(t)s, %(e)s, 'draft', %(v)s) RETURNING tenant),"
        " v AS ("
        "  INSERT INTO experiment_versions (tenant, experiment_id, experiment_version, variant)"
        "  VALUES (%(t)s, %(e)s, %(v)s, '20-percent-off') RETURNING tenant)"
        " INSERT INTO draft_revisions"
        "  (tenant, experiment_id, experiment_version, revision_no, hypothesis)"
        " VALUES (%(t)s, %(e)s, %(v)s, 1, 'a discount retains at-risk customers')",
        {"t": TENANT, "e": experiment, "v": version},
    )


def event(
    db: Database, kind: str, payload: str = '{"percentage": 10}', follows: int | None = 0
) -> int:
    """One event, as raw SQL. `follows` is the state it moved from (migrations/0015)."""
    row = db.fetch_one(
        "INSERT INTO registry_events"
        " (tenant, experiment_id, experiment_version, kind, payload, follows)"
        " VALUES (%s, %s, %s, %s, %s::jsonb, %s) RETURNING id",
        (TENANT, EXPERIMENT, VERSION, kind, payload, follows),
    )
    assert row is not None
    return int(row[0])


def refused_by(constraint: str, db: Database, sql: str) -> None:
    with pytest.raises(IntegrityViolation) as caught:
        db.execute(sql)
    assert caught.value.constraint == constraint, str(caught.value)


def test_a_draft_lands_as_one_statement(app_database: Database) -> None:
    seed_draft(app_database)

    row = app_database.fetch_one("SELECT status, current_version FROM experiments")
    assert row == ("draft", VERSION)


# --- the lifecycle is closed ---


def test_a_status_outside_the_lifecycle_is_refused(app_database: Database) -> None:
    """`approved` is not a registry state - approval lives in `approvals` - and
    `concluded` is iteration 2. A status nothing handles is a status nothing leaves."""
    seed_draft(app_database)

    refused_by(
        "experiment_status_is_known",
        app_database,
        "UPDATE experiments SET status = 'approved'",
    )


def test_an_experiment_cannot_point_at_a_version_that_does_not_exist(
    app_database: Database,
) -> None:
    refused_by(
        "current_version_exists",
        app_database,
        "INSERT INTO experiments (tenant, experiment_id, status, current_version)"
        " VALUES ('acme', 'exp-9', 'draft', 'exp:nowhere')",
    )


def test_history_cannot_name_an_unknown_version(app_database: Database) -> None:
    seed_draft(app_database)

    with pytest.raises(IntegrityViolation):
        app_database.execute(
            "INSERT INTO registry_events (tenant, experiment_id, experiment_version, kind, payload)"
            " VALUES ('acme', 'exp-7', 'exp:other', 'halt', '{}'::jsonb)"
        )


def test_an_event_kind_outside_the_known_effects_is_refused(app_database: Database) -> None:
    seed_draft(app_database)

    with pytest.raises(IntegrityViolation) as caught:
        event(app_database, "update")
    assert caught.value.constraint == "registry_event_kind_is_known"


# --- history is insert-only ---

HISTORY = {
    "experiment_versions": "variant = 'something else'",
    "draft_revisions": "hypothesis = 'rewritten'",
    "registry_events": "payload = '{}'::jsonb",
}


@pytest.mark.parametrize("table", sorted(HISTORY))
def test_history_cannot_be_rewritten(app_database: Database, table: str) -> None:
    seed_draft(app_database)
    event(app_database, "rollout")

    refused_by(
        "registry_history_is_insert_only",
        app_database,
        f"UPDATE {table} SET {HISTORY[table]}",
    )


@pytest.mark.parametrize("table", sorted(HISTORY))
def test_history_cannot_be_deleted(app_database: Database, table: str) -> None:
    seed_draft(app_database)
    event(app_database, "rollout")

    refused_by("registry_history_is_insert_only", app_database, f"DELETE FROM {table}")


# --- the same effect twice is one row ---


def test_two_rollouts_from_the_same_state_are_refused(app_database: Database) -> None:
    """The store's own copy of what the idempotency key means (migrations/0015). The
    surface never sees the key, so it holds unique what the key is made of: where the
    move started. Two rollouts from one prior - whatever they say - are a fork, and
    history is a line."""
    seed_draft(app_database)
    event(app_database, "rollout", '{"percentage": 10}')

    with pytest.raises(IntegrityViolation) as caught:
        event(app_database, "rollout", '{"percentage": 25}')
    assert caught.value.constraint == "one_rollout_per_prior"


def test_a_return_to_an_earlier_percentage_is_a_new_row(app_database: Database) -> None:
    """Audit C2: 10%, 25%, then 10% again is three rollouts. 0012 keyed on the payload,
    and the approved ramp-down collided with the first rollout."""
    seed_draft(app_database)
    first = event(app_database, "rollout", '{"percentage": 10}', follows=0)
    second = event(app_database, "rollout", '{"percentage": 25}', follows=first)
    event(app_database, "rollout", '{"percentage": 10}', follows=second)

    row = app_database.fetch_one("SELECT count(*) FROM registry_events")
    assert row == (3,)


def test_a_rollout_must_say_where_it_started(app_database: Database) -> None:
    """A NULL is distinct in a unique index, so a rollout that named no prior would slip
    past `one_rollout_per_prior` every time."""
    seed_draft(app_database)

    with pytest.raises(IntegrityViolation) as caught:
        event(app_database, "rollout", follows=None)
    assert caught.value.constraint == "a_move_names_where_it_started"


def test_a_version_ends_once(app_database: Database) -> None:
    seed_draft(app_database)
    event(app_database, "halt", '{"percentage": 0, "reason": "a"}', follows=None)

    with pytest.raises(IntegrityViolation) as caught:
        event(app_database, "halt", '{"percentage": 0, "reason": "b"}', follows=None)
    assert caught.value.constraint == "one_ending_per_version"


def test_two_revisions_of_the_same_revision_are_refused(app_database: Database) -> None:
    """A revision's prior is its `revision_no` minus one, so the key is the fork check."""
    seed_draft(app_database)
    revise = (
        "INSERT INTO draft_revisions"
        " (tenant, experiment_id, experiment_version, revision_no, hypothesis)"
        " VALUES ('acme', 'exp-7', 'exp:abc123', 2, %s)"
    )
    app_database.execute(revise, ("a smaller discount retains them too",))

    with pytest.raises(IntegrityViolation) as caught:
        app_database.execute(revise, ("a third wording",))
    assert caught.value.constraint == "draft_revisions_pkey"


def test_an_earlier_wording_can_come_back(app_database: Database) -> None:
    """Audit C2: A, B, A is three revisions and the draft says A. 0012 held the wording
    unique per version, and going back was refused or swallowed."""
    seed_draft(app_database)
    for no, text in [(2, "a smaller discount retains them too"), (3, SEED_HYPOTHESIS)]:
        app_database.execute(
            "INSERT INTO draft_revisions"
            " (tenant, experiment_id, experiment_version, revision_no, hypothesis)"
            " VALUES ('acme', 'exp-7', 'exp:abc123', %s, %s)",
            (no, text),
        )

    row = app_database.fetch_one("SELECT count(*) FROM draft_revisions")
    assert row == (3,)


def test_tenants_do_not_share_an_experiment_id(app_database: Database) -> None:
    """`exp-7` at two tenants is two experiments. A key without the tenant would make
    the second tenant's draft collide with - or worse, update - the first's."""
    seed_draft(app_database)
    app_database.execute(
        "WITH e AS ("
        "  INSERT INTO experiments (tenant, experiment_id, status, current_version)"
        "  VALUES ('globex', 'exp-7', 'draft', 'exp:abc123') RETURNING tenant)"
        " INSERT INTO experiment_versions (tenant, experiment_id, experiment_version, variant)"
        " VALUES ('globex', 'exp-7', 'exp:abc123', 'x')"
    )

    row = app_database.fetch_one("SELECT count(*) FROM experiments")
    assert row == (2,)


# --- the client contract: the fake and the real store behave the same (T25) ---
#
# `build_stack` wires Postgres; the fake remains for tests that want no database. One
# suite over both is what keeps "the fake says nothing reached the surface" meaning the
# same thing as "nothing reached the registry".

DRAFT_AT = f"{TENANT}/experiments/{EXPERIMENT}"
ROLLOUT_AT = f"{DRAFT_AT}/rollout"
DRAFT_PAYLOAD = {
    "experiment_version": VERSION,
    "hypothesis": "a discount retains at-risk customers",
    "variant": "20-percent-off",
}


def rollout_payload(
    percentage: int = 10, version: str = VERSION, prior: int = 0, threshold: float = 0.61
) -> dict[str, object]:
    return {
        "experiment_version": version,
        "percentage": percentage,
        "targeting_model_version": "tabpfn-3.5",
        "risk_threshold": threshold,
        "prior_rollout_event": prior,
    }


def roll_out(client: Any, percentage: int, **kwargs: Any) -> str:
    """A rollout from wherever the experiment is now, read the way the model reads it."""
    prior = client.read(f"{DRAFT_AT}/history", {}).get("latest_rollout_event", 0)
    return str(client.commit(ROLLOUT_AT, rollout_payload(percentage, prior=prior, **kwargs)))


@pytest.fixture(params=["fake", "postgres"])
def client(request: pytest.FixtureRequest, app_database: Database) -> SurfaceClient:
    if request.param == "fake":
        return RegistryClient()
    return PostgresRegistryClient(db=app_database)


def test_a_draft_can_be_read_back(client: Any) -> None:
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)

    assert client.read(DRAFT_AT, {}) == {
        "experiment_id": EXPERIMENT,
        "status": "draft",
        "experiment_version": VERSION,
        "hypothesis": DRAFT_PAYLOAD["hypothesis"],
        "variant": DRAFT_PAYLOAD["variant"],
        # What a revision names as its prior.
        "revision_no": 1,
    }
    assert client.drafts == {DRAFT_AT: DRAFT_PAYLOAD}


def test_reading_an_experiment_that_does_not_exist_is_empty(client: Any) -> None:
    assert client.read(DRAFT_AT, {}) == {}


def test_a_draft_over_an_existing_experiment_is_refused(client: Any) -> None:
    """Redrafting under a new version is assumption 6's relaunch path, and no tool owns
    it yet. Until one does, the draft tool cannot touch an experiment that exists."""
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)

    with pytest.raises(SurfaceRefused):
        client.commit(DRAFT_AT, {**DRAFT_PAYLOAD, "experiment_version": "exp:other"})

    assert client.read(DRAFT_AT, {})["experiment_version"] == VERSION


def test_a_rollout_of_a_draft_makes_it_live(client: Any) -> None:
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)

    client.commit(ROLLOUT_AT, rollout_payload())

    assert client.read(DRAFT_AT, {})["status"] == "live"
    assert client.rollouts == [(ROLLOUT_AT, rollout_payload())]


def test_a_live_experiment_can_be_widened(client: Any) -> None:
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)
    roll_out(client, 10)

    roll_out(client, 25)

    assert [p["percentage"] for _, p in client.rollouts] == [10, 25]


# --- audit C2: a move is keyed on where it started ---


def test_a_ramp_down_to_an_earlier_percentage_lands(client: Any) -> None:
    """10%, 25%, then back to 10%, each approved by a person. Keyed on the target, the
    third was the first one's duplicate and exposure stayed at 25%."""
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)
    receipts = [roll_out(client, pct) for pct in (10, 25, 10)]

    history = client.read(HISTORY_AT, {})
    assert len(set(receipts)) == 3
    assert [e["percentage"] for e in history["events"]] == [10, 25, 10]
    assert history["current_exposure"] == 10


def test_the_same_percentage_for_a_different_cohort_lands(client: Any) -> None:
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)
    roll_out(client, 10)

    roll_out(client, 10, threshold=0.7)

    assert [p["risk_threshold"] for _, p in client.rollouts] == [0.61, 0.7]


def test_two_rollouts_approved_against_one_state_land_once(client: Any) -> None:
    """Both were prepared from the same history. The first lands; the second no longer
    follows the latest rollout, and nothing of it applies."""
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)
    roll_out(client, 10)
    prior = client.read(HISTORY_AT, {})["latest_rollout_event"]

    client.commit(ROLLOUT_AT, rollout_payload(25, prior=prior))
    with pytest.raises(SurfaceRefused):
        client.commit(ROLLOUT_AT, rollout_payload(50, prior=prior))

    assert [p["percentage"] for _, p in client.rollouts] == [10, 25]
    assert client.read(HISTORY_AT, {})["current_exposure"] == 25


@pytest.mark.parametrize("prior", [7, 1000])
def test_a_rollout_from_a_state_that_never_was_is_refused(client: Any, prior: int) -> None:
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)

    with pytest.raises(SurfaceRefused):
        client.commit(ROLLOUT_AT, rollout_payload(prior=prior))

    assert client.rollouts == []
    assert client.read(DRAFT_AT, {})["status"] == "draft"


def test_history_names_each_event_and_the_positions_a_move_follows(client: Any) -> None:
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)
    roll_out(client, 10)
    first = client.read(HISTORY_AT, {})
    client.commit(ABSTENTION_AT, abstention_payload(prior=first["latest_event"]))

    after = client.read(HISTORY_AT, {})

    assert [e["event_id"] for e in after["events"]] == [first["latest_rollout_event"]]
    assert after["latest_rollout_event"] == first["latest_rollout_event"]
    assert after["latest_event"] > first["latest_event"], "an abstention is a position too"


def test_a_rollout_of_nothing_is_refused(client: Any) -> None:
    with pytest.raises(SurfaceRefused):
        client.commit(ROLLOUT_AT, rollout_payload())

    assert client.rollouts == []


def test_a_rollout_of_a_version_that_is_not_current_is_refused(client: Any) -> None:
    """The approval was granted against one frozen cohort. A rollout naming another
    must not land on this experiment."""
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)

    with pytest.raises(SurfaceRefused):
        client.commit(ROLLOUT_AT, rollout_payload(version="exp:other"))

    assert client.rollouts == []
    assert client.read(DRAFT_AT, {})["status"] == "draft"


def test_the_same_rollout_twice_is_answered_with_its_receipt_and_recorded_once(
    client: Any,
) -> None:
    """Audit M4: the same move from the same state is on record, so it happened. Refusing
    it would say "nothing applied" about an effect the store can prove applied."""
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)
    first = client.commit(ROLLOUT_AT, rollout_payload())
    roll_out(client, 25)

    again = client.commit(ROLLOUT_AT, rollout_payload())

    assert again == first
    assert len(client.rollouts) == 2
    assert client.read(HISTORY_AT, {})["current_exposure"] == 25, "the answer moved nothing"


def test_a_resource_the_registry_does_not_serve_is_refused(client: Any) -> None:
    with pytest.raises(SurfaceRefused):
        client.commit(f"{DRAFT_AT}/delete_everything", {})

    assert client.drafts == {}


def test_tenants_are_separate_registries(client: Any) -> None:
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)

    assert client.read(f"globex/experiments/{EXPERIMENT}", {}) == {}


def test_the_rollout_resource_is_written_not_read(client: Any) -> None:
    """History is read through its own resource (T26), not by reading the verb."""
    with pytest.raises(SurfaceRefused):
        client.read(ROLLOUT_AT, {})


# --- reads (T26) ---

LIST_AT = f"{TENANT}/experiments/"
HISTORY_AT = f"{DRAFT_AT}/history"


def draft(client: Any, experiment: str, tenant: str = TENANT) -> None:
    client.commit(f"{tenant}/experiments/{experiment}", DRAFT_PAYLOAD)


@pytest.mark.parametrize("resource", [LIST_AT, HISTORY_AT])
def test_a_read_resource_is_read_not_written(client: Any, resource: str) -> None:
    """Each resource serves the verbs in the table and no other: committing to history
    or to the collection would be a write with no precondition to hold it."""
    draft(client, EXPERIMENT)

    with pytest.raises(SurfaceRefused):
        client.commit(resource, {"experiment_version": VERSION})


def test_a_list_names_each_experiment_once(client: Any) -> None:
    draft(client, "exp-2")
    draft(client, "exp-1")

    assert client.read(LIST_AT, {"limit": 20}) == {
        "experiments": [
            {"experiment_id": "exp-1", "status": "draft", "experiment_version": VERSION},
            {"experiment_id": "exp-2", "status": "draft", "experiment_version": VERSION},
        ]
    }


def test_a_list_filters_by_status(client: Any) -> None:
    draft(client, "exp-1")
    draft(client, EXPERIMENT)
    roll_out(client, 10)

    live = client.read(LIST_AT, {"status": "live", "limit": 20})["experiments"]

    assert [row["experiment_id"] for row in live] == [EXPERIMENT]


def test_a_list_is_one_tenants(client: Any) -> None:
    draft(client, "exp-1")
    draft(client, "exp-9", tenant="globex")

    rows = client.read(LIST_AT, {"limit": 20})["experiments"]

    assert [row["experiment_id"] for row in rows] == ["exp-1"]


@pytest.mark.parametrize(("asked", "returned"), [(2, 2), (5000, 3), (0, 1)])
def test_a_list_is_clamped_at_the_surface(client: Any, asked: int, returned: int) -> None:
    """The schema refuses more than 50; the surface clamps anyway, because the schema is
    not its only caller."""
    for n in range(3):
        draft(client, f"exp-{n}")

    assert len(client.read(LIST_AT, {"limit": asked})["experiments"]) == returned


def test_history_is_in_order_and_derives_exposure(client: Any) -> None:
    draft(client, EXPERIMENT)
    roll_out(client, 10)
    roll_out(client, 25)

    history = client.read(HISTORY_AT, {})

    assert [e["percentage"] for e in history["events"]] == [10, 25]
    assert {e["kind"] for e in history["events"]} == {"rollout"}
    assert history["current_exposure"] == 25


def test_a_draft_has_an_empty_history(client: Any) -> None:
    draft(client, EXPERIMENT)

    assert client.read(HISTORY_AT, {}) == {
        "experiment_id": EXPERIMENT,
        "events": [],
        "current_exposure": 0,
        "latest_rollout_event": 0,
        "latest_event": 0,
    }


def test_the_history_of_nothing_is_empty(client: Any) -> None:
    assert client.read(HISTORY_AT, {}) == {}


# --- the draft lifecycle (T27) ---

REVISION_AT = f"{DRAFT_AT}/revision"
DISCARD_AT = f"{DRAFT_AT}/discard"
HALT_AT = f"{DRAFT_AT}/halt"
REWORDED = "a smaller discount retains them too"


def halt_payload(version: str = VERSION) -> dict[str, object]:
    return {"experiment_version": version, "percentage": 0, "reason": "churn rose in the variant"}


def revise_payload(
    hypothesis: str = REWORDED, version: str = VERSION, prior: int = 1
) -> dict[str, object]:
    return {"experiment_version": version, "hypothesis": hypothesis, "prior_revision": prior}


def revise(client: Any, hypothesis: str) -> str:
    """A revision of whatever the draft says now, read the way the model reads it."""
    prior = client.read(DRAFT_AT, {})["revision_no"]
    return str(client.commit(REVISION_AT, revise_payload(hypothesis, prior=prior)))


def discard_payload(version: str = VERSION) -> dict[str, object]:
    return {"experiment_version": version, "reason": "the cohort is too small"}


def bring_to(client: Any, state: str) -> None:
    """Drive the experiment to `state` through the client's own writes, never around them."""
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)
    if state in ("live", "halted"):
        client.commit(ROLLOUT_AT, rollout_payload())
    if state == "halted":
        client.commit(HALT_AT, halt_payload())
    elif state == "discarded":
        client.commit(DISCARD_AT, discard_payload())


def observed(client: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Everything a caller can see about the experiment: a refusal must change none of it."""
    return client.read(DRAFT_AT, {}), client.read(HISTORY_AT, {})


# Every state but the one each action needs.
NOT_A_DRAFT = ["live", "halted", "discarded"]
NOT_LIVE = ["draft", "halted", "discarded"]


def test_a_revision_rewords_the_draft_and_keeps_its_version(client: Any) -> None:
    bring_to(client, "draft")

    client.commit(REVISION_AT, revise_payload())

    after = client.read(DRAFT_AT, {})
    assert (after["hypothesis"], after["experiment_version"]) == (REWORDED, VERSION)
    assert after["status"] == "draft"
    assert client.drafts == {DRAFT_AT: DRAFT_PAYLOAD}, "what was drafted is still on record"


def test_a_revision_is_a_row_never_a_version(app_database: Database) -> None:
    """Assumption 5: the version is the frozen cohort. Rewording the reason must not
    mint a new one - that would be a different experiment wearing this one's id."""
    client = PostgresRegistryClient(db=app_database)
    bring_to(client, "draft")

    client.commit(REVISION_AT, revise_payload())

    assert app_database.fetch_one("SELECT count(*) FROM experiment_versions") == (1,)
    assert app_database.fetch_all(
        "SELECT revision_no, hypothesis FROM draft_revisions ORDER BY revision_no"
    ) == [(1, DRAFT_PAYLOAD["hypothesis"]), (2, REWORDED)]


def test_the_same_revision_twice_is_one_revision(client: Any) -> None:
    """The same wording from the same revision is a move already made: its receipt is
    the answer (audit M4), and there is still one row."""
    bring_to(client, "draft")
    first = client.commit(REVISION_AT, revise_payload())

    again = client.commit(REVISION_AT, revise_payload())

    assert again == first
    assert client.read(DRAFT_AT, {})["revision_no"] == 2


def test_an_earlier_wording_can_be_revised_back_to(client: Any) -> None:
    """Audit C2: A, B, A. Keyed on the wording, the third was refused or swallowed and
    the draft stayed at B."""
    bring_to(client, "draft")
    receipts = [revise(client, REWORDED), revise(client, DRAFT_PAYLOAD["hypothesis"])]

    after = client.read(DRAFT_AT, {})
    assert len(set(receipts)) == 2
    assert (after["hypothesis"], after["revision_no"]) == (DRAFT_PAYLOAD["hypothesis"], 3)


def test_two_revisions_of_one_revision_land_once(client: Any) -> None:
    """Both reworded revision 1. The first lands; the second was written against a
    wording that is no longer the latest, and nothing of it applies."""
    bring_to(client, "draft")
    client.commit(REVISION_AT, revise_payload(REWORDED, prior=1))

    with pytest.raises(SurfaceRefused):
        client.commit(REVISION_AT, revise_payload("a third wording", prior=1))

    assert client.read(DRAFT_AT, {})["hypothesis"] == REWORDED


def test_a_blank_revision_is_refused_not_left_unresolved(client: Any) -> None:
    """The store's CHECK fails the whole statement, so nothing applied - a refusal the
    gateway can release, not an unknown outcome that strands the claim."""
    bring_to(client, "draft")

    with pytest.raises(SurfaceRefused, match="must say something"):
        client.commit(REVISION_AT, revise_payload(hypothesis="   "))

    assert client.read(DRAFT_AT, {})["hypothesis"] == DRAFT_PAYLOAD["hypothesis"]


@pytest.mark.parametrize("state", NOT_A_DRAFT)
def test_a_revision_is_refused_outside_a_draft(client: Any, state: str) -> None:
    bring_to(client, state)
    before = observed(client)

    with pytest.raises(SurfaceRefused):
        client.commit(REVISION_AT, revise_payload())

    assert observed(client) == before


def test_a_revision_of_another_version_is_refused(client: Any) -> None:
    bring_to(client, "draft")

    with pytest.raises(SurfaceRefused):
        client.commit(REVISION_AT, revise_payload(version="exp:other"))

    assert client.read(DRAFT_AT, {})["hypothesis"] == DRAFT_PAYLOAD["hypothesis"]


def test_a_revision_of_nothing_is_refused(client: Any) -> None:
    with pytest.raises(SurfaceRefused):
        client.commit(REVISION_AT, revise_payload())


def test_a_discard_ends_a_draft(client: Any) -> None:
    bring_to(client, "draft")

    client.commit(DISCARD_AT, discard_payload())

    assert client.read(DRAFT_AT, {})["status"] == "discarded"
    discarded = client.read(LIST_AT, {"status": "discarded", "limit": 20})["experiments"]
    assert [row["experiment_id"] for row in discarded] == [EXPERIMENT]


@pytest.mark.parametrize("state", NOT_A_DRAFT)
def test_a_discard_is_refused_outside_a_draft(client: Any, state: str) -> None:
    """A live experiment is stopped by halting it, which says what exposure became. A
    discard of something customers saw would erase that it ever went out."""
    bring_to(client, state)
    before = observed(client)

    with pytest.raises(SurfaceRefused):
        client.commit(DISCARD_AT, discard_payload())

    assert observed(client) == before


def test_a_discard_of_another_version_is_refused(client: Any) -> None:
    bring_to(client, "draft")

    with pytest.raises(SurfaceRefused):
        client.commit(DISCARD_AT, discard_payload(version="exp:other"))

    assert client.read(DRAFT_AT, {})["status"] == "draft"


def test_a_discarded_experiment_cannot_be_rolled_out(client: Any) -> None:
    bring_to(client, "discarded")

    with pytest.raises(SurfaceRefused):
        client.commit(ROLLOUT_AT, rollout_payload())

    assert client.rollouts == []


# --- the abstention record (T28) ---

ABSTENTION_AT = f"{DRAFT_AT}/abstention"

# Every state the lifecycle can reach.
EVERY_STATE = ["draft", "live", "halted", "discarded"]


NOISE = "the guardrail metric is inside its noise band"


def abstention_payload(
    explanation: str = NOISE, version: str = VERSION, prior: int = 0
) -> dict[str, object]:
    return {"experiment_version": version, "explanation": explanation, "prior_event": prior}


def abstain(client: Any, explanation: str = NOISE) -> str:
    """An abstention by an evaluation that has just read the history."""
    prior = client.read(HISTORY_AT, {})["latest_event"]
    return str(client.commit(ABSTENTION_AT, abstention_payload(explanation, prior=prior)))


def abstentions(client: Any, app_database: Database) -> int:
    """How many abstentions reached the store, asked of the store for either client."""
    if isinstance(client, RegistryClient):
        return sum(1 for _, kind, _ in client.events if kind == "abstention")
    row = app_database.fetch_one("SELECT count(*) FROM registry_events WHERE kind = 'abstention'")
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("state", EVERY_STATE)
def test_an_abstention_is_recorded_in_every_state_and_moves_none(
    client: Any, app_database: Database, state: str
) -> None:
    """Assumption 7: a record, not a transition. The run stops because the policy said
    abstain, not because a row was written - so writing one changes nothing it could see."""
    bring_to(client, state)
    experiment, history = observed(client)

    abstain(client)

    after, history_after = observed(client)
    assert abstentions(client, app_database) == 1
    assert after == experiment, "an abstention moves no status"
    # The one thing it moves is the position a next abstention names: that is how the
    # next cycle's record is told from this one's.
    assert history_after["latest_event"] > history["latest_event"]
    assert {**history_after, "latest_event": 0} == {**history, "latest_event": 0}, (
        "an abstention is not rollout history and changes no exposure"
    )


def test_the_same_abstention_twice_is_recorded_once(client: Any, app_database: Database) -> None:
    """The same explanation against the same read is one cycle's record, retried: its
    receipt is the answer (audit M4)."""
    bring_to(client, "live")
    prior = client.read(HISTORY_AT, {})["latest_event"]
    first = client.commit(ABSTENTION_AT, abstention_payload(prior=prior))

    again = client.commit(ABSTENTION_AT, abstention_payload(prior=prior))

    assert again == first
    abstain(client, "a later cycle, a different reason")
    assert abstentions(client, app_database) == 2


def test_a_later_cycle_saying_the_same_thing_is_recorded(
    client: Any, app_database: Database
) -> None:
    """Audit C2: "insufficient sample" is what every early cycle says. Keyed on the
    words, the second cycle to say it left no record."""
    bring_to(client, "live")

    receipts = [abstain(client, "insufficient sample"), abstain(client, "insufficient sample")]

    assert len(set(receipts)) == 2
    assert abstentions(client, app_database) == 2


def test_two_abstentions_against_one_read_land_once(client: Any, app_database: Database) -> None:
    bring_to(client, "live")
    prior = client.read(HISTORY_AT, {})["latest_event"]
    client.commit(ABSTENTION_AT, abstention_payload("one reason", prior=prior))

    with pytest.raises(SurfaceRefused):
        client.commit(ABSTENTION_AT, abstention_payload("another reason", prior=prior))

    assert abstentions(client, app_database) == 1


def test_an_abstention_on_a_version_that_does_not_exist_is_refused(
    client: Any, app_database: Database
) -> None:
    bring_to(client, "live")

    with pytest.raises(SurfaceRefused):
        client.commit(ABSTENTION_AT, abstention_payload(version="exp:other"))

    assert abstentions(client, app_database) == 0


# --- the halt (T29) ---


def test_a_halt_stops_a_live_experiment_at_zero(client: Any) -> None:
    bring_to(client, "live")

    client.commit(HALT_AT, halt_payload())

    assert client.read(DRAFT_AT, {})["status"] == "halted"
    history = client.read(HISTORY_AT, {})
    assert [e["kind"] for e in history["events"]] == ["rollout", "halt"]
    assert history["current_exposure"] == 0, "exposure is derived from the halt, not stored"


@pytest.mark.parametrize("state", NOT_LIVE)
def test_a_halt_is_refused_unless_live(client: Any, state: str) -> None:
    """A draft has nothing to stop; a halted or discarded version is terminal."""
    bring_to(client, state)
    before = observed(client)

    with pytest.raises(SurfaceRefused):
        client.commit(HALT_AT, halt_payload())

    assert observed(client) == before


def test_a_halt_of_another_version_is_refused(client: Any) -> None:
    bring_to(client, "live")

    with pytest.raises(SurfaceRefused):
        client.commit(HALT_AT, halt_payload(version="exp:other"))

    assert client.read(DRAFT_AT, {})["status"] == "live"


def test_a_halted_version_cannot_be_rolled_out_again(client: Any) -> None:
    """Assumption 6: relaunching is a new version and a fresh approval, never an un-halt."""
    bring_to(client, "halted")

    with pytest.raises(SurfaceRefused):
        client.commit(ROLLOUT_AT, rollout_payload(25))

    assert len(client.rollouts) == 1
    assert client.read(DRAFT_AT, {})["status"] == "halted"


# --- each act's payload is held to its shape (C1) ---
#
# The resource names the act; the payload must be that act's, exactly. The C1 exploit
# wrote a draft's payload to `/rollout`, and the rollout event it left had no
# percentage - a row `get_rollout_history` then raised a KeyError on. Both clients
# refuse such a payload before touching anything, and the store refuses the row.

NEW_DRAFT_AT = f"{TENANT}/experiments/exp-9"

MALFORMED: dict[str, tuple[str, str, dict[str, object]]] = {
    "a draft's payload, sent as a rollout": ("live", ROLLOUT_AT, DRAFT_PAYLOAD),
    "a rollout with no percentage": (
        "live",
        ROLLOUT_AT,
        {k: v for k, v in rollout_payload().items() if k != "percentage"},
    ),
    "a rollout past 100": ("live", ROLLOUT_AT, rollout_payload(150)),
    "a rollout below 0": ("live", ROLLOUT_AT, rollout_payload(-5)),
    "a rollout to True": ("live", ROLLOUT_AT, {**rollout_payload(), "percentage": True}),
    "a rollout to 10.5": ("live", ROLLOUT_AT, {**rollout_payload(), "percentage": 10.5}),
    "a rollout with a string risk": (
        "live",
        ROLLOUT_AT,
        {**rollout_payload(), "risk_threshold": "high"},
    ),
    "a rollout with an extra key": ("live", ROLLOUT_AT, {**rollout_payload(), "cohort": "all"}),
    "a halt to 5%": ("live", HALT_AT, {**halt_payload(), "percentage": 5}),
    "a halt with no percentage": ("live", HALT_AT, {"experiment_version": VERSION, "reason": "r"}),
    "a draft with no variant": (
        "draft",
        NEW_DRAFT_AT,
        {"experiment_version": VERSION, "hypothesis": "h"},
    ),
    "a draft with a blank hypothesis": (
        "draft",
        NEW_DRAFT_AT,
        {**DRAFT_PAYLOAD, "hypothesis": " "},
    ),
    "a revision with a variant": ("draft", REVISION_AT, {**revise_payload(), "variant": "x"}),
    "a discard with no reason": ("draft", DISCARD_AT, {"experiment_version": VERSION}),
    "an abstention that is a number": (
        "live",
        ABSTENTION_AT,
        {**abstention_payload(), "explanation": 7},
    ),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_payload_is_refused_and_changes_nothing(client: Any, case: str) -> None:
    state, resource, payload = MALFORMED[case]
    bring_to(client, state)

    def everything() -> tuple[Any, ...]:
        return observed(client), client.read(LIST_AT, {"limit": 50}), list(client.rollouts)

    before = everything()

    with pytest.raises(SurfaceRefused, match="nothing applied"):
        client.commit(resource, payload)

    assert everything() == before


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("rollout", "{}"),
        ("rollout", '{"percentage": 150}'),
        ("rollout", '{"percentage": -1}'),
        ("rollout", '{"percentage": 10.5}'),
        ("rollout", '{"percentage": "10"}'),
        ("rollout", '{"percentage": true}'),
        ("halt", "{}"),
        ("halt", '{"percentage": 5}'),
        ("halt", '{"percentage": "0"}'),
    ],
)
def test_the_store_refuses_an_exposure_event_without_its_percentage(
    app_database: Database, kind: str, payload: str
) -> None:
    """`migrations/0014`: the store's copy of the clients' shape rule, for the writer
    that is not a client."""
    seed_draft(app_database)

    with pytest.raises(IntegrityViolation) as caught:
        event(app_database, kind, payload)
    assert (
        caught.value.constraint
        == {
            "rollout": "rollout_names_a_whole_percentage",
            "halt": "halt_names_zero",
        }[kind]
    )


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("rollout", '{"percentage": 0}'),
        ("rollout", '{"percentage": 100}'),
        ("halt", '{"percentage": 0}'),
    ],
)
def test_the_store_accepts_an_exposure_event_at_its_bounds(
    app_database: Database, kind: str, payload: str
) -> None:
    seed_draft(app_database)

    event(app_database, kind, payload)

    assert app_database.fetch_one("SELECT count(*) FROM registry_events") == (1,)
