"""Time one local TabPFN fold on a stratified slice of the KKBox cohort (K12).

    uv run --extra prediction python scripts/time_kkbox_fold.py --rows 2000
    uv run --extra prediction python scripts/time_kkbox_fold.py --rows 5000

One process per size, so peak memory is that size's own. Prints seconds and peak RSS and
writes nothing: the numbers go in ADR-0012 by hand. A full run is `folds` times one fold's
fit+predict, at fold size (folds-1)/folds of the cut.
"""

from __future__ import annotations

import argparse
import resource
import sys
import time

from agentstack.context import datasets
from agentstack.prediction.churn import DEFAULT_FOLDS, DEFAULT_SEED, fold_assignment
from agentstack.prediction.engine import TabPFNScorer, encode
from agentstack.prediction.licence import check_token, load_env


def device() -> str:
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps (available; TabPFN picks its own)"
    return "cpu"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, required=True, help="cohort rows to draw")
    args = parser.parse_args(argv)
    load_env()
    check_token()

    snapshot = datasets.load("kkbox-churn")
    target = datasets.REGISTRY["kkbox-churn"].target
    frame = snapshot.frame.sample(n=args.rows, random_state=DEFAULT_SEED)
    labels = frame[target].reset_index(drop=True)
    features = encode(frame.drop(columns=[target]).reset_index(drop=True))
    assignment = fold_assignment([int(v) for v in labels], folds=DEFAULT_FOLDS, seed=DEFAULT_SEED)
    held = [i for i, f in enumerate(assignment) if f == 0]
    train = [i for i, f in enumerate(assignment) if f != 0]

    classifier = TabPFNScorer()._classifier()
    start = time.perf_counter()
    classifier.fit(features.iloc[train], labels.take(train))
    fitted = time.perf_counter()
    classifier.predict_proba(features.iloc[held])
    done = time.perf_counter()

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30  # bytes on macOS
    print(f"device     {getattr(classifier, 'device_', None) or device()}")
    print(f"rows       {args.rows}  (fit {len(train)}, predict {len(held)})")
    print(f"fit        {fitted - start:.1f} s")
    print(f"predict    {done - fitted:.1f} s")
    total = (done - start) * DEFAULT_FOLDS / 60
    print(f"fold       {done - start:.1f} s   x{DEFAULT_FOLDS} = {total:.1f} min")
    print(f"peak RSS   {peak:.2f} GiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
