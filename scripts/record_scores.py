"""Record real TabPFN scores so CI can exercise the adapter without the weights.

    export TABPFN_TOKEN=...
    uv sync --extra prediction
    uv run python scripts/record_scores.py

Writes data/scores/<dataset>.json, which IS committed - unlike the datasets themselves.
That file is what lets CI check our feature handling, our fold assignment and our
replay guards on numbers that a real model produced, without installing torch or
holding a licence.

What it does not do is prove the model still works. Nothing recorded can. That is what
`tests/live/test_real_scores.py` is for, and it needs a token.
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

    scorer = TabPFNScorer()
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
