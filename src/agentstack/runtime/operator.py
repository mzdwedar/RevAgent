"""Trigger to candidate: the path Checkpoint C asked for.

`runtime.cycles.evaluate` takes its evaluator as a seam. This is what fills it - the
step that turns "something happened at this watermark" into "here is a cohort worth
experimenting on, or here is why not".

What it does **not** do is draft. Drafting is the model's job and it happens in a turn,
through the registry, the exposure filter, policy and the gateway. This produces the
frozen cohort a draft is written *about*, and stops there, because the point of the
targeting being deterministic is that no model gets a say in who enters the experiment.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentstack.context import datasets
from agentstack.context.targeting import Cohort, TargetingRefused, TargetingRule, select
from agentstack.policy.triggers import Outcome, TriggerEvent
from agentstack.prediction.churn import ChurnScorer, ScoringError


class TriggerNotUnderstood(RuntimeError):
    """The trigger names a watermark this system cannot resolve to a cohort."""


@dataclass(frozen=True, slots=True)
class Evaluation:
    """One cycle's conclusion, and the evidence behind it."""

    outcome: Outcome
    cohort: Cohort | None
    reason: str

    def as_seam_result(self) -> tuple[Outcome, str | None]:
        """The shape `cycles.evaluate` expects."""
        return self.outcome, None if self.cohort is None else self.cohort.experiment_version


def dataset_of(data_as_of: str) -> str:
    """The dataset a watermark belongs to.

    `data_as_of` is `{dataset}:{digest}` by construction (`context.datasets.watermark`),
    so this is reading it rather than guessing. A watermark that does not carry its
    dataset could not identify a population on its own, which is the one thing it is for.
    """
    dataset, separator, _ = data_as_of.partition(":")
    if not separator or not dataset:
        raise TriggerNotUnderstood(
            f"{data_as_of!r} is not a watermark this system produced; expected "
            "'{dataset}:{digest}'"
        )
    return dataset


def evaluate_trigger(
    trigger: TriggerEvent,
    *,
    scorer: ChurnScorer,
    rule: TargetingRule | None = None,
) -> Evaluation:
    """Score the cohort this trigger points at and decide whether it is worth running.

    Every refusal below is an *answer*, not an error: a cohort too small, too cheap, or
    on a dataset with no observed revenue is a conclusion the cycle records and moves
    on from. Only a watermark that cannot be resolved at all is a failure, because then
    there is no population to have concluded anything about.
    """
    rule = rule or TargetingRule.load()
    key = dataset_of(trigger.data_as_of)

    try:
        snapshot = datasets.load(key)
    except datasets.DatasetError as exc:
        raise TriggerNotUnderstood(f"{trigger.data_as_of}: {exc}") from exc

    if snapshot.data_as_of != trigger.data_as_of:
        # The trigger fired on a batch, and what is on disk is a different one. Scoring
        # the wrong population would produce a cohort that looks frozen and is not.
        return Evaluation(
            outcome=Outcome.ABSTAIN,
            cohort=None,
            reason=(
                f"the trigger names {trigger.data_as_of} and the snapshot on disk is "
                f"{snapshot.data_as_of}; refusing to target a population nobody asked about"
            ),
        )

    target_column = datasets.REGISTRY[key].target
    try:
        scores = scorer.score(
            features=snapshot.frame.drop(columns=[target_column]),
            labels=snapshot.frame[target_column],
            dataset=key,
            data_as_of=snapshot.data_as_of,
        )
    except ScoringError as exc:
        raise TriggerNotUnderstood(f"{trigger.data_as_of} could not be scored: {exc}") from exc

    try:
        cohort = select(snapshot, scores, rule=rule)
    except TargetingRefused as exc:
        return Evaluation(outcome=Outcome.ABSTAIN, cohort=None, reason=str(exc))

    return Evaluation(
        outcome=Outcome.PROPOSE,
        cohort=cohort,
        reason=(
            f"{cohort.size} customers above {cohort.risk_threshold:.3f}, "
            f"${cohort.annual_value_at_risk_cents / 100:,.0f} at risk"
        ),
    )
