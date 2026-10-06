"""The conclusion of a measured experiment, as a record (migration 0022, layer 5).

Written once per (experiment version, endpoint) and never edited. A second write of the same
key returns the first, which is what makes an at-least-once conclude step produce exactly
one conclusion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentstack.storage.database import Database

_COLUMNS = (
    "tenant, experiment_id, experiment_version, endpoint, action, reason, simulated, readout, rule"
)


@dataclass(frozen=True, slots=True)
class StoredConclusion:
    tenant: str
    experiment_id: str
    experiment_version: str
    endpoint: int
    action: str
    reason: str
    simulated: bool
    readout: dict[str, Any]
    rule: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConclusionStore:
    db: Database

    def record(self, conclusion: StoredConclusion) -> StoredConclusion:
        """Write it, or return it as it was first written."""
        self.db.execute(
            f"INSERT INTO conclusions ({_COLUMNS})"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)"
            " ON CONFLICT (tenant, experiment_id, experiment_version, endpoint) DO NOTHING",
            (
                conclusion.tenant,
                conclusion.experiment_id,
                conclusion.experiment_version,
                conclusion.endpoint,
                conclusion.action,
                conclusion.reason,
                conclusion.simulated,
                json.dumps(conclusion.readout),
                json.dumps(conclusion.rule),
            ),
        )
        stored = self.get(
            tenant=conclusion.tenant,
            experiment_id=conclusion.experiment_id,
            experiment_version=conclusion.experiment_version,
            endpoint=conclusion.endpoint,
        )
        assert stored is not None  # just written, or already there
        return stored

    def get(
        self, *, tenant: str, experiment_id: str, experiment_version: str, endpoint: int
    ) -> StoredConclusion | None:
        row = self.db.fetch_one(
            f"SELECT {_COLUMNS} FROM conclusions"
            " WHERE tenant = %s AND experiment_id = %s AND experiment_version = %s"
            " AND endpoint = %s",
            (tenant, experiment_id, experiment_version, endpoint),
        )
        return None if row is None else StoredConclusion(*row)
