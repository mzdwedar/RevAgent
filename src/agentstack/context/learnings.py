"""What a concluded experiment leaves in memory (L6, layer 5).

Two records, split by who wrote the bytes. The first is ours: the numbers come from the
stored conclusion's Readout and the cohort's frozen description, formatted here, and it is
`FIRST_PARTY`. The second is the experiment's hypothesis, which a model wrote, kept apart
and `UNTRUSTED` so it can be recalled quoted and never as a fact. Nothing a model said
reaches the first record, and nothing else in the system writes learnings.

Written through `memory.write()` like everything else, once per conclusion: the key is the
conclusion's identity, so the at-least-once conclude step that calls this can run again and
find the records already there. Only a final conclusion is learned from. An `extend` says
"not yet", which is not something to recall.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from agentstack.context.conclusion_store import StoredConclusion
from agentstack.context.items import Scope, Trust
from agentstack.context.memory import MemoryRecord, MemoryStore, write
from agentstack.storage.database import Database

LEARNING_TTL = timedelta(days=180)
MAX_HYPOTHESIS = 500


def conclusion_id(c: StoredConclusion) -> str:
    """The readout's identity: the same one the conclusions table is keyed on."""
    return f"readout:{c.tenant}/{c.experiment_id}/{c.experiment_version}/{c.endpoint}"


def learning_key(c: StoredConclusion) -> str:
    return f"learning:{c.experiment_id}:{c.experiment_version}:{c.endpoint}"


def hypothesis_key(c: StoredConclusion) -> str:
    return f"learning-hypothesis:{c.experiment_id}:{c.experiment_version}:{c.endpoint}"


def first_party_value(c: StoredConclusion, *, cohort: dict[str, Any], percentage: int) -> str:
    """The numbers, from the Readout and the frozen cohort only."""
    r = c.readout
    adjusted = r.get("adjusted", {})
    tau = float(adjusted.get("tau_cents", 0.0))
    se = float(adjusted.get("se_cents", 0.0))
    return json.dumps(
        {
            "action": c.action,
            "cohort": cohort,
            "offer": r.get("offer"),
            "offer_cost_cents_per_customer": r.get("offer_cost_cents"),
            "percentage": percentage,
            "tau_cents": tau,
            "tau_ci95_cents": [tau - 1.96 * se, tau + 1.96 * se],
            "insv_cents": r.get("insv_cents"),
            "insv_ci95_cents": [r.get("insv_lower_cents"), r.get("insv_upper_cents")],
            "covariate_correlation": r.get("covariate_correlation"),
            "simulated": c.simulated,
            "experiment_version": c.experiment_version,
        },
        sort_keys=True,
        default=str,
    )


@dataclass(frozen=True, slots=True)
class Learnings:
    db: Database

    def hypothesis(self, c: StoredConclusion) -> str | None:
        """The model-written hypothesis the experiment was drafted with, as last revised."""
        row = self.db.fetch_one(
            "SELECT hypothesis FROM draft_revisions"
            " WHERE tenant = %s AND experiment_id = %s AND experiment_version = %s"
            " ORDER BY revision_no DESC LIMIT 1",
            (c.tenant, c.experiment_id, c.experiment_version),
        )
        return None if row is None else str(row[0])

    def learn(
        self, c: StoredConclusion, *, cohort: dict[str, Any], percentage: int
    ) -> list[MemoryRecord]:
        """Write what this conclusion taught, once. Returns the records written now."""
        if c.action == "extend":
            return []
        store = MemoryStore(db=self.db)
        scope = Scope(tenant=c.tenant)
        provenance = conclusion_id(c)
        written: list[MemoryRecord] = []
        if not store.holds(c.tenant, learning_key(c)):
            written.append(
                write(
                    store,
                    key=learning_key(c),
                    value=first_party_value(c, cohort=cohort, percentage=percentage),
                    scope=scope,
                    provenance=provenance,
                    ttl=LEARNING_TTL,
                    explicit=True,
                )
            )
        said = self.hypothesis(c)
        if said and not store.holds(c.tenant, hypothesis_key(c)):
            written.append(
                write(
                    store,
                    key=hypothesis_key(c),
                    value=said[:MAX_HYPOTHESIS],
                    scope=scope,
                    provenance=f"{provenance} (hypothesis, model-written)",
                    ttl=LEARNING_TTL,
                    explicit=True,
                    trust=Trust.UNTRUSTED,
                )
            )
        return written
