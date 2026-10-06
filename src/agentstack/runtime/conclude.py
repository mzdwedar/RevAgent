"""Measure one rolled-out experiment and conclude it (L5, layer 3).

The orchestration the conclude activity runs. It is plain code so it can be tested without
Temporal: the workflow supplies the clock (a durable timer) and the activity calls this.

The cohort is not stored with its members, so it is frozen again from the recorded scores
under the same rule, and the result must carry the same experiment version. A version names
one population: if it no longer comes out the same, the data under it moved, and a readout
of some other population is refused, not reported.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from pathlib import Path

from agentstack.context import datasets
from agentstack.context.assignment import Assignment
from agentstack.context.conclusion_store import ConclusionStore, StoredConclusion
from agentstack.context.frozen_cohorts import FrozenCohortStore
from agentstack.context.outcomes import load_outcomes, outcomes_path
from agentstack.context.readout import readout
from agentstack.context.targeting import TargetingRule, select
from agentstack.policy.conclusions import ConclusionRule, conclude
from agentstack.prediction.churn import ChurnScorer
from agentstack.prediction.covariate import eltv_pre
from agentstack.runtime.drafting import ROLLOUT_PERCENTAGE

PAYMENT_COLUMN = "last_actual_amount_paid"


class ConclusionRefused(RuntimeError):
    """The experiment cannot be measured as the version that was rolled out."""


def conclude_experiment(
    *,
    cohorts: FrozenCohortStore,
    store: ConclusionStore,
    scorer: ChurnScorer,
    tenant: str,
    experiment_id: str,
    experiment_version: str,
    rollout_on: date,
    endpoint: int,
    outcomes_root: Path,
    rule: ConclusionRule | None = None,
    targeting: TargetingRule | None = None,
) -> StoredConclusion:
    """The conclusion for this endpoint: the stored one if it exists, else measured now."""
    existing = store.get(
        tenant=tenant,
        experiment_id=experiment_id,
        experiment_version=experiment_version,
        endpoint=endpoint,
    )
    if existing is not None:
        return existing

    frozen = cohorts.get(
        tenant=tenant, experiment_id=experiment_id, experiment_version=experiment_version
    )
    if frozen is None:
        raise ConclusionRefused(f"no frozen cohort for {experiment_id} {experiment_version}")

    key = str(frozen.description["dataset"])
    snapshot = datasets.load(key)
    if snapshot.data_as_of != frozen.data_as_of:
        raise ConclusionRefused(
            f"the data is now {snapshot.data_as_of}, the cohort was frozen on {frozen.data_as_of}"
        )
    if PAYMENT_COLUMN not in snapshot.frame.columns:
        raise ConclusionRefused(f"{key} has no {PAYMENT_COLUMN}, so no covariate can be built")

    target = datasets.REGISTRY[key].target
    scores = scorer.score(
        features=snapshot.frame.drop(columns=[target]),
        labels=snapshot.frame[target],
        dataset=key,
        data_as_of=snapshot.data_as_of,
    )
    # The worker's own targeting rule when it has one (as evaluation uses), else the named
    # profile the cohort was frozen under.
    profile = str(frozen.description["targeting_profile"])
    cohort = select(snapshot, scores, rule=targeting or TargetingRule.load(profile))
    if cohort.experiment_version != experiment_version:
        raise ConclusionRefused(
            f"the cohort now freezes as {cohort.experiment_version}, not {experiment_version}"
        )

    payments = (snapshot.frame[PAYMENT_COLUMN].fillna(0) * 100).round().astype("int64")
    covariate = eltv_pre(scores, payments.tolist(), data_as_of=snapshot.data_as_of)
    outcomes = load_outcomes(
        outcomes_path(outcomes_root, experiment_id, experiment_version),
        cohort,
        rollout_on=rollout_on,
    )
    assignment = Assignment(cohort, ROLLOUT_PERCENTAGE)
    measured = readout(outcomes, assignment, covariate)
    decided = conclude(measured, extensions_used=endpoint, rule=rule or ConclusionRule.load())

    return store.record(
        StoredConclusion(
            tenant=tenant,
            experiment_id=experiment_id,
            experiment_version=experiment_version,
            endpoint=endpoint,
            action=decided.action,
            reason=decided.reason,
            simulated=decided.simulated,
            readout=asdict(measured),
            rule=decided.rule.record(),
        )
    )
