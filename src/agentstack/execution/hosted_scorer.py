"""The hosted churn scorer: PriorLabs' TabPFN service, cross-fitted over the cohort.

Layer 7, not 4b. Scoring through this module sends cohort rows to a third party, which is
egress, and only `agentstack.execution` may hold a real surface client (`.importlinter`
contract 3, `tabpfn_client` included since K7). Scoring is still a *read*: it changes nothing
in the world, so it takes no idempotency-ledger entry and no approval.

What stays ours, so the out-of-fold property is checkable even though the model is not:

* the fold assignment (`fold_assignment`),
* the encoding (`encode`),
* the refusal to record a score under a model version nobody reported.

The vendor only fits and predicts. A failure is a `ScoringError`: never retried here, never
degraded to a guess, and the cohort is not left half-scored. (The client library has its own
transport-level retries; those are the vendor's and sit below this seam.)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import pandas as pd

from agentstack.prediction.churn import (
    DEFAULT_FOLDS,
    DEFAULT_SEED,
    ChurnScores,
    ScoringError,
    fold_assignment,
)
from agentstack.prediction.engine import CHECKPOINT, encode
from agentstack.prediction.licence import check_token

# The one host cohort rows may be sent to. `tabpfn-client` takes its base URL from a bundled
# config but lets `TABPFN_CLIENT_API_URL` override it, so the check reads what the client
# *will use* rather than what we expect it to.
PRIORLABS_HOST = "api.priorlabs.ai"
PRIORLABS_PORT = 443

MODEL_VERSION_PREFIX = "priorlabs:"


class HostedClassifier(Protocol):
    """The verified `tabpfn-client` surface (ADR-0012 4a), plus the version read-back."""

    def fit(self, features: pd.DataFrame, labels: pd.Series) -> Any: ...

    def predict_proba(self, features: pd.DataFrame) -> Any: ...

    def served_model_version(self) -> str | None:
        """What the service says it ran, after a predict; `None` if it did not say."""
        ...


class PriorLabsClassifier:
    """Adapts a `tabpfn_client.TabPFNClassifier` to `HostedClassifier`.

    `model_path` is `"<version>_default"`, which the *server* resolves to a checkpoint, so
    the request names a version and the response is the only evidence of what ran.
    """

    def __init__(self, underlying: Any) -> None:
        self._underlying = underlying

    def fit(self, features: pd.DataFrame, labels: pd.Series) -> Any:
        return self._underlying.fit(features, labels)

    def predict_proba(self, features: pd.DataFrame) -> Any:
        return self._underlying.predict_proba(features)

    def served_model_version(self) -> str | None:
        # `_last_meta` is private to the client: it has no public accessor for the predict
        # metadata. It is the only place the server's statement lives, and it is a plain
        # dict, so a missing or renamed key reads as "not reported" and the scorer refuses.
        meta = getattr(self._underlying, "_last_meta", None) or {}
        billing = meta.get("billing_model_version")
        package = meta.get("package_version")
        if not billing or not package:
            return None
        return f"{billing}+{package}"


def _build_priorlabs_classifier(checkpoint: str) -> HostedClassifier:
    try:
        from tabpfn_client import TabPFNClassifier
    except ImportError as exc:
        raise ScoringError(
            "tabpfn-client is not installed. It is an optional extra: uv sync --extra prediction"
        ) from exc
    return PriorLabsClassifier(TabPFNClassifier.create_default_for_version(checkpoint))


def _client_endpoint() -> str:
    try:
        from tabpfn_client.client import ServiceClient
    except ImportError as exc:
        raise ScoringError(
            "tabpfn-client is not installed. It is an optional extra: uv sync --extra prediction"
        ) from exc
    return str(ServiceClient.base_url)


def check_host(endpoint: str) -> None:
    """Refuse any destination other than PriorLabs over TLS, before a row is sent."""
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != PRIORLABS_HOST
        or (parsed.port or PRIORLABS_PORT) != PRIORLABS_PORT
    ):
        raise ScoringError(
            f"the hosted client would send cohort rows to {endpoint!r}; only "
            f"https://{PRIORLABS_HOST}:{PRIORLABS_PORT} is allowed. "
            "Is TABPFN_CLIENT_API_URL set?"
        )


@dataclass(frozen=True, slots=True)
class HostedTabPFNScorer:
    folds: int = DEFAULT_FOLDS
    seed: int = DEFAULT_SEED
    # Both are seams, as `TabPFNScorer.build_classifier` is: the fold loop and the refusals
    # are tested with a fake that mirrors the verified surface, and the real client is only
    # ever exercised against the real service (tests/live, K12).
    build_classifier: Callable[[str], HostedClassifier] | None = None
    endpoint: Callable[[], str] | None = None

    @property
    def model_version(self) -> str:
        # The version *requested*. What a score is recorded under is what was served.
        return f"{MODEL_VERSION_PREFIX}{CHECKPOINT}"

    def score(
        self,
        *,
        features: pd.DataFrame,
        labels: pd.Series,
        dataset: str,
        data_as_of: str,
    ) -> ChurnScores:
        """Out-of-fold churn probability for every row, from the hosted service."""
        check_token()
        if len(features) != len(labels):
            raise ScoringError(f"{len(features)} rows of features, {len(labels)} labels")
        check_host((self.endpoint or _client_endpoint)())

        encoded = encode(features)
        assignment = fold_assignment([int(v) for v in labels], folds=self.folds, seed=self.seed)
        probabilities = [0.0] * len(encoded)
        versions: set[str] = set()

        for fold in range(self.folds):
            held_out = [i for i, f in enumerate(assignment) if f == fold]
            conditioned_on = [i for i, f in enumerate(assignment) if f != fold]
            if not held_out:
                continue
            classifier = (self.build_classifier or _build_priorlabs_classifier)(CHECKPOINT)
            try:
                classifier.fit(encoded.iloc[conditioned_on], labels.take(conditioned_on))
                predicted = classifier.predict_proba(encoded.iloc[held_out])
            except ScoringError:
                raise
            except Exception as exc:
                raise ScoringError(
                    f"hosted scoring failed on fold {fold} of {self.folds}: {exc}"
                ) from exc

            reported = (classifier.served_model_version() or "").strip()
            if not reported:
                raise ScoringError(
                    "the hosted service did not report which model version it ran, so these "
                    "scores cannot be recorded under one. Refusing rather than guessing."
                )
            if versions and reported not in versions:
                raise ScoringError(
                    f"the hosted service changed model version mid-cohort ({sorted(versions)} "
                    f"then {reported}); one cohort cannot be scored by two models."
                )
            versions.add(reported)
            for position, row in enumerate(held_out):
                probabilities[row] = float(predicted[position][1])

        # `fold_assignment` refuses fewer rows than folds, so at least one fold ran and
        # exactly one version was recorded: anything else was refused above.
        (served,) = versions

        return ChurnScores(
            dataset=dataset,
            data_as_of=data_as_of,
            model_version=f"{MODEL_VERSION_PREFIX}{served}",
            folds=self.folds,
            seed=self.seed,
            probabilities=tuple(probabilities),
            positives=int(labels.sum()),
        )
