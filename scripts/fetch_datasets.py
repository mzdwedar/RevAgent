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

# KKBox is a Kaggle *competition*, not a dataset, and its raw files are archives. Only the
# files the cohort needs are fetched: `user_logs*` is ~30 GB and out of scope. v2 transactions
# are mostly the label window (March 2017), so the history comes from v1 `transactions.csv`.
KKBOX_COMPETITION = "kkbox-churn-prediction-challenge"
KKBOX_FILES = (
    "WSDMChurnLabeller.scala",
    "train_v2.csv.7z",
    "transactions_v2.csv.7z",
    "transactions.csv.7z",
    "members_v3.csv.7z",
)


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


def fetch_kkbox(into: Path) -> None:
    """Download and unpack the KKBox raw files into `into` (gitignored). Needs the competition
    rules accepted on Kaggle; unpacking uses `bsdtar`, which reads 7z and ships with macOS."""
    into.mkdir(parents=True, exist_ok=True)
    print(f"----- kkbox raw  ({KKBOX_COMPETITION})")
    for name in KKBOX_FILES:
        target = into / name.removesuffix(".7z")
        if target.exists():
            print(f"  {target.name} already present")
            continue
        subprocess.run(
            ["uv", "run", "kaggle", "competitions", "download", "-c", KKBOX_COMPETITION]
            + ["-f", name, "-p", str(into)],
            cwd=ROOT,
            check=True,
        )
        if name.endswith(".7z"):
            subprocess.run(["bsdtar", "-xf", str(into / name), "-C", str(into)], check=True)
    # The v2 archives unpack into a nested folder; flatten it.
    for nested in (into / "data" / "churn_comp_refresh").glob("*.csv"):
        nested.replace(into / nested.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="rewrite data/manifest.json")
    parser.add_argument("--data", type=Path, default=datasets.DATA_ROOT)
    parser.add_argument("--kkbox", action="store_true", help="fetch the KKBox raw files and stop")
    args = parser.parse_args()

    if args.kkbox:
        fetch_kkbox(args.data / "kkbox-raw")
        return 0

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
