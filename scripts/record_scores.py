"""Record real TabPFN scores from the local model, one file per cohort.

    uv sync --extra prediction
    uv run python scripts/record_scores.py --datasets kkbox-churn

`TABPFN_TOKEN` is read from `.env` (an exported one wins). Always the local scorer: no
row leaves the machine, and there is no switch to the hosted one (ADR-0012 6).

Writes data/scores/<dataset>.json, which is NOT committed: `data/*` is git-ignored, and
the scores are a function of third-party rows. The file is what lets `RecordedScorers`
replay real numbers without the weights, on the machine that recorded them.

A KKBox run is long (about 42 minutes at the 50,000-row cut, ADR-0012 6). It prints each
fold as it finishes and writes a dataset's file only once every fold is scored, so a
died run leaves nothing a later run could mistake for a result.

What it does not do is prove the model still works. Nothing recorded can. That is what
`tests/live/test_real_scores.py` is for.
"""

from __future__ import annotations

import argparse
import json

from agentstack.context import datasets
from agentstack.prediction.churn import SCORES_ROOT
from agentstack.prediction.engine import TabPFNScorer
from agentstack.prediction.licence import load_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", nargs="*", default=sorted(datasets.REGISTRY))
    args = parser.parse_args(argv)
    load_env()

    scorer = TabPFNScorer(progress=lambda done, total: print(f"  fold {done}/{total}", flush=True))
    print(f"----- preflight\nok    {scorer.preflight()}")
    SCORES_ROOT.mkdir(parents=True, exist_ok=True)

    for key in args.datasets:
        snapshot = datasets.load(key)
        target = datasets.REGISTRY[key].target
        print(f"----- {key}  ({snapshot.rows} rows, {snapshot.data_as_of})")
        scored = scorer.score(
            features=snapshot.frame.drop(columns=[target]),
            labels=snapshot.frame[target],
            dataset=key,
            data_as_of=snapshot.data_as_of,
        )
        path = SCORES_ROOT / f"{key}.json"
        path.write_text(
            json.dumps(
                {
                    "dataset": scored.dataset,
                    "data_as_of": scored.data_as_of,
                    "model_version": scored.model_version,
                    "folds": scored.folds,
                    "seed": scored.seed,
                    "positives": scored.positives,
                    "probabilities": [round(p, 6) for p in scored.probabilities],
                },
                indent=2,
            )
            + "\n"
        )
        print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
