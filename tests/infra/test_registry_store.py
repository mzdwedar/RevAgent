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


def event(db: Database, kind: str, payload: str = '{"percentage": 10}') -> None:
    db.execute(
        "INSERT INTO registry_events (tenant, experiment_id, experiment_version, kind, payload)"
        " VALUES (%s, %s, %s, %s, %s::jsonb)",
        (TENANT, EXPERIMENT, VERSION, kind, payload),
    )


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


def test_the_same_effect_twice_is_refused(app_database: Database) -> None:
    """The store's own copy of what the idempotency key means. The surface never sees
    the key, so the identity of the effect - its kind and payload, per version - is what
    it can hold unique."""
    seed_draft(app_database)
    event(app_database, "rollout")

    with pytest.raises(IntegrityViolation) as caught:
        event(app_database, "rollout")
    assert caught.value.constraint == "one_row_per_effect"


def test_a_different_effect_of_the_same_kind_is_a_second_row(app_database: Database) -> None:
    """10% then 25% are two rollouts, exactly as their idempotency keys say."""
    seed_draft(app_database)
    event(app_database, "rollout", '{"percentage": 10}')
    event(app_database, "rollout", '{"percentage": 25}')

    row = app_database.fetch_one("SELECT count(*) FROM registry_events")
    assert row == (2,)


def test_the_same_revision_text_twice_is_refused(app_database: Database) -> None:
    seed_draft(app_database)

    refused_by(
        "one_row_per_revision",
        app_database,
        "INSERT INTO draft_revisions"
        " (tenant, experiment_id, experiment_version, revision_no, hypothesis)"
        " VALUES ('acme', 'exp-7', 'exp:abc123', 2, 'a discount retains at-risk customers')",
    )


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


def rollout_payload(percentage: int = 10, version: str = VERSION) -> dict[str, object]:
    return {
        "experiment_version": version,
        "percentage": percentage,
        "targeting_model_version": "tabpfn-3.5",
        "risk_threshold": 0.61,
    }


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
    client.commit(ROLLOUT_AT, rollout_payload(10))

    client.commit(ROLLOUT_AT, rollout_payload(25))

    assert [p["percentage"] for _, p in client.rollouts] == [10, 25]


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


def test_the_same_rollout_twice_is_refused_and_recorded_once(client: Any) -> None:
    client.commit(DRAFT_AT, DRAFT_PAYLOAD)
    client.commit(ROLLOUT_AT, rollout_payload())

    with pytest.raises(SurfaceRefused):
        client.commit(ROLLOUT_AT, rollout_payload())

    assert len(client.rollouts) == 1


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
    client.commit(ROLLOUT_AT, rollout_payload())

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
    client.commit(ROLLOUT_AT, rollout_payload(10))
    client.commit(ROLLOUT_AT, rollout_payload(25))

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


def revise_payload(hypothesis: str = REWORDED, version: str = VERSION) -> dict[str, object]:
    return {"experiment_version": version, "hypothesis": hypothesis}


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


def test_the_same_wording_twice_is_one_revision(client: Any) -> None:
    bring_to(client, "draft")
    client.commit(REVISION_AT, revise_payload())

    with pytest.raises(SurfaceRefused):
        client.commit(REVISION_AT, revise_payload())


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


def abstention_payload(
    explanation: str = "the guardrail metric is inside its noise band", version: str = VERSION
) -> dict[str, object]:
    return {"experiment_version": version, "explanation": explanation}


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
    before = observed(client)

    client.commit(ABSTENTION_AT, abstention_payload())

    assert abstentions(client, app_database) == 1
    assert observed(client) == before, "an abstention is not rollout history and moves no status"


def test_the_same_abstention_twice_is_recorded_once(client: Any, app_database: Database) -> None:
    bring_to(client, "live")
    client.commit(ABSTENTION_AT, abstention_payload())

    with pytest.raises(SurfaceRefused):
        client.commit(ABSTENTION_AT, abstention_payload())

    client.commit(ABSTENTION_AT, abstention_payload("a later cycle, a different reason"))
    assert abstentions(client, app_database) == 2


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
