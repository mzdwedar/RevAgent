"""`eLTV_pre`: the pre-treatment covariate for the readout (layer 4).

    eLTV_pre = (1 - cross-fitted churn probability) * last observed monthly payment

Both factors are known at freeze time. The churn probability is the recorded, out-of-fold
score the cohort was already targeted with, so no row is predicted by a model that saw its
own label. The payment is an observed column. That gives the ANCOVA a regressor correlated
with the outcome (renewal times revenue) without being touched by the offer.

The plan called for a TabPFN spend regressor too. Next month's payment is the last
month's, so a regressor would add a second 40-minute run for no information. It is left
out, and the covariate needs no new recording or token.

The covariate is a regressor only. It never enters INSV, which reads observed outcomes
(SPEC criterion 12).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agentstack.prediction.churn import ChurnScores, ScoringError

DEFINITION = "(1 - churn_probability) * last_actual_amount_paid_cents"


@dataclass(frozen=True, slots=True)
class Covariate:
    dataset: str
    data_as_of: str
    model_version: str
    definition: str
    values: tuple[float, ...]

    def provenance(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "data_as_of": self.data_as_of,
            "model_version": self.model_version,
            "definition": self.definition,
        }


def eltv_pre(scores: ChurnScores, payment_cents: Sequence[int], *, data_as_of: str) -> Covariate:
    """The covariate for every scored row, or refuse if it is not for this snapshot."""
    if scores.data_as_of != data_as_of:
        raise ScoringError(
            f"scores are for {scores.data_as_of}, cohort is {data_as_of}; a covariate "
            "from another snapshot is not pre-treatment for this one"
        )
    if len(payment_cents) != len(scores.probabilities):
        raise ScoringError(f"{len(payment_cents)} payments for {len(scores.probabilities)} scores")
    return Covariate(
        dataset=scores.dataset,
        data_as_of=scores.data_as_of,
        model_version=scores.model_version,
        definition=DEFINITION,
        values=tuple(
            (1.0 - p) * pay for p, pay in zip(scores.probabilities, payment_cents, strict=True)
        ),
    )
