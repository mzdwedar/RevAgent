"""The frozen cohort a proposal rests on, as a record (T40b, migration 0017).

`targeting.select` freezes a cohort and names it with an experiment version. The rest of
the run needs more than the name, long after the process that scored it is gone: the
rollout carries the cohort's predicate in its payload, and the approver is shown how
many customers and how much revenue the rollout would touch. So when a cycle proposes,
the cohort is written down here, once, and never edited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentstack.context.targeting import Cohort
from agentstack.storage.database import Database

_COLUMNS = (
    "tenant, experiment_id, experiment_version, data_as_of, targeting_model_version, "
    "risk_threshold, size, annual_value_at_risk_cents, description"
)


@dataclass(frozen=True, slots=True)
class FrozenCohort:
    tenant: str
    experiment_id: str
    experiment_version: str
    data_as_of: str
    targeting_model_version: str
    risk_threshold: float
    size: int
    annual_value_at_risk_cents: int
    description: dict[str, Any]

    @property
    def currency(self) -> str:
        """Rows frozen before currency existed carry no key; every one of them was USD."""
        return str(self.description.get("currency", "USD"))


@dataclass(frozen=True, slots=True)
class FrozenCohortStore:
    db: Database

    def record(self, *, tenant: str, experiment_id: str, cohort: Cohort) -> FrozenCohort:
        """Write it, or return it as it was first written: a version names one cohort."""
        described = cohort.description()
        self.db.execute(
            f"INSERT INTO frozen_cohorts ({_COLUMNS})"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
            " ON CONFLICT (tenant, experiment_id, experiment_version) DO NOTHING",
            (
                tenant,
                experiment_id,
                cohort.experiment_version,
                cohort.data_as_of,
                cohort.model_version,
                cohort.risk_threshold,
                cohort.size,
                cohort.annual_value_at_risk_cents,
                json.dumps(described),
            ),
        )
        frozen = self.get(
            tenant=tenant, experiment_id=experiment_id, experiment_version=cohort.experiment_version
        )
        assert frozen is not None  # just written, or already there
        return frozen

    def get(
        self, *, tenant: str, experiment_id: str, experiment_version: str
    ) -> FrozenCohort | None:
        row = self.db.fetch_one(
            f"SELECT {_COLUMNS} FROM frozen_cohorts"
            " WHERE tenant = %s AND experiment_id = %s AND experiment_version = %s",
            (tenant, experiment_id, experiment_version),
        )
        return None if row is None else FrozenCohort(*row)
