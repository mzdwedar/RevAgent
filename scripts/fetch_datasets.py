"""Fetch the cohort datasets from Kaggle and record what arrived.

Outside the package on purpose. This reaches the network, and in this codebase only
`agentstack.execution` does that - `lint-imports` contract 5 now names `kaggle` so the
rule is checked rather than remembered.

    uv run python scripts/fetch_datasets.py            # both default cohorts
    uv run python scripts/fetch_datasets.py --record   # and update data/manifest.json

Needs Kaggle credentials in ~/.kaggle/kaggle.json. The datasets are third-party and
licensed, so `data/` is gitignored; `data/manifest.json` is committed instead, and a
changed `data_as_of` in a diff is how a change upstream becomes visible.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from agentstack.context import datasets

ROOT = Path(__file__).resolve().parents[1]


def fetch(spec: datasets.DatasetSpec, into: Path) -> None:
    print(f"----- {spec.key}  ({spec.kaggle})")
    if all((into / name).exists() for name in spec.files):
        print("  already present")
        return
    subprocess.run(
        [
            "uv",
            "run",
            "kaggle",
            "datasets",
            "download",
            "-d",
            spec.kaggle,
            "-p",
            str(into),
            "--unzip",
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="rewrite data/manifest.json")
    parser.add_argument("--data", type=Path, default=datasets.DATA_ROOT)
    args = parser.parse_args()

    args.data.mkdir(parents=True, exist_ok=True)
    for spec in datasets.REGISTRY.values():
        fetch(spec, args.data)

    manifest = {}
    for key in datasets.REGISTRY:
        snapshot = datasets.load(key, root=args.data)
        manifest[key] = snapshot.summary()
        print(f"  {key}: {snapshot.rows} rows, churn {snapshot.churn_rate:.3f}")
        print(f"    data_as_of {snapshot.data_as_of}")

    if args.record:
        target = args.data / "manifest.json"
        target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        print(f"\nrecorded {target}")
    else:
        print("\n(run with --record to write data/manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
