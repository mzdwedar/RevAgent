"""A draft records the offer the model chose and what it costs (D2, ADR-0015).

The cost is the registry's own: computed from the frozen cohort and the policy bounds, so
the figure an approver is later shown is one nobody argued into the row.
"""

from __future__ import annotations

import pytest

from agentstack.execution.surfaces import PostgresRegistryClient, SurfaceRefused
from agentstack.storage.database import Database

TENANT, EXPERIMENT, VERSION = "acme", "exp-7", "exp:offer1"
AT = f"{TENANT}/experiments/{EXPERIMENT}"


def draft(**changes: object) -> dict[str, object]:
    return {
        "experiment_version": VERSION,
        "hypothesis": "a discount retains at-risk customers",
        "variant": "20-percent-off",
        "offer_kind": "percent_off",
        "offer_amount": 20,
        "offer_months": 3,
    } | changes


def freeze(db: Database) -> None:
    db.execute(
        "INSERT INTO frozen_cohorts (tenant, experiment_id, experiment_version, data_as_of,"
        " targeting_model_version, risk_threshold, size, annual_value_at_risk_cents, description)"
        " VALUES (%s, %s, %s, 'fixture:1', 'tabpfn-3.5', 0.24, 1000, 200000, '{}'::jsonb)",
        (TENANT, EXPERIMENT, VERSION),
    )


def test_a_draft_stores_its_offer_and_the_cost_the_registry_computed(
    app_database: Database,
) -> None:
    freeze(app_database)

    PostgresRegistryClient(db=app_database).commit(AT, draft())

    # ARPU is 200 cents a year; 20% off for 3 months is 10 cents a customer. The widest
    # arm policy allows is 15% of 1000 = 150 customers.
    row = app_database.fetch_one(
        "SELECT offer_kind, offer_amount, offer_months, cost_per_customer_cents,"
        " total_cost_cents FROM experiment_versions WHERE experiment_id = %s",
        (EXPERIMENT,),
    )
    assert row == ("percent_off", 20, 3, 10, 1500)


def test_an_offer_with_no_frozen_cohort_to_cost_it_is_refused(app_database: Database) -> None:
    with pytest.raises(SurfaceRefused, match="no frozen cohort"):
        PostgresRegistryClient(db=app_database).commit(AT, draft())


def test_an_offer_outside_the_bounds_is_refused_by_the_store_too(app_database: Database) -> None:
    freeze(app_database)

    with pytest.raises(SurfaceRefused, match="percent_off"):
        PostgresRegistryClient(db=app_database).commit(AT, draft(offer_amount=90))


def test_a_draft_without_an_offer_is_still_a_draft(app_database: Database) -> None:
    payload = draft()
    for key in ("offer_kind", "offer_amount", "offer_months"):
        del payload[key]

    PostgresRegistryClient(db=app_database).commit(AT, payload)

    row = app_database.fetch_one(
        "SELECT offer_kind, total_cost_cents FROM experiment_versions WHERE experiment_id = %s",
        (EXPERIMENT,),
    )
    assert row == (None, None)


def test_a_half_an_offer_is_malformed(app_database: Database) -> None:
    payload = draft()
    del payload["offer_months"]

    with pytest.raises(SurfaceRefused, match="exactly"):
        PostgresRegistryClient(db=app_database).commit(AT, payload)
