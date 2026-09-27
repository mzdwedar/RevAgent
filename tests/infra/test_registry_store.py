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
