"""The raw-date comparison, once (K13): the two candidate columns, with and without.

    uv run --extra prediction python scripts/compare_kkbox_raw_dates.py

Local only: the local `TabPFNScorer` on a seeded, stratified sample of the real raw files, so no
row leaves the machine (ADR-0012 section 6). Writes nothing; the numbers go in ADR-0012 by hand.

Declared before it was run: the sample is `USERS` users drawn with `SEED`; both arms see the same
rows, folds and seed, so the only difference is the two columns. The arm with the lower
out-of-fold log loss wins, and a difference under `TIE` goes to without, since a column that
buys nothing is a column to explain. A candidate also has to pass `kkbox.candidate_refusals`.
This is run once and is not repeated until one arm wins.
"""

from __future__ import annotations

import sys

import pandas as pd
from build_kkbox_cohort import RAW, build
from sklearn.metrics import log_loss, roc_auc_score

from agentstack.context import kkbox
from agentstack.prediction.engine import TabPFNScorer
from agentstack.prediction.licence import check_token, load_env

USERS = 5_000
SEED = kkbox.COHORT_SEED
TIE = 0.0005


def score(frame: pd.DataFrame, scorer: TabPFNScorer) -> tuple[float, float]:
    """Out-of-fold log loss and AUC of a cohort frame, `msno` already the index."""
    labels = frame[kkbox.TARGET]
    scores = scorer.score(
        features=frame.drop(columns=[kkbox.TARGET]),
        labels=labels,
        dataset="kkbox-churn",
        data_as_of="k13-comparison",
    )
    probabilities = list(scores.probabilities)
    return float(log_loss(labels, probabilities)), float(roc_auc_score(labels, probabilities))


def main() -> int:
    load_env()
    check_token()
    frame = build(RAW, USERS, SEED, raw_dates=True).set_index("msno")
    refused = kkbox.candidate_refusals(frame)
    print(f"rows {len(frame)}  churn {frame[kkbox.TARGET].mean():.3f}  refused {refused or 'none'}")
    for column in kkbox.CANDIDATE_COLUMNS:
        corr = abs(frame[column].corr(frame[kkbox.TARGET]))
        print(f"|corr| {column} with {kkbox.TARGET}: {corr:.3f}")

    scorer = TabPFNScorer()
    without = score(frame.drop(columns=list(kkbox.CANDIDATE_COLUMNS)), scorer)
    kept = frame.drop(columns=list(refused))
    with_dates = score(kept, scorer)
    print(f"without  log loss {without[0]:.5f}  AUC {without[1]:.4f}")
    print(f"with     log loss {with_dates[0]:.5f}  AUC {with_dates[1]:.4f}")
    gain = without[0] - with_dates[0]
    wins = bool(gain > TIE) and not refused
    print(f"gain {gain:+.5f} (tie band {TIE})  ->  {'WITH' if wins else 'WITHOUT'} wins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
