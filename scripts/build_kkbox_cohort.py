"""Build `data/kkbox-cohort.csv` from the raw KKBox files, offline.

Outside the package on purpose: it reads third-party files, and the cohort it writes is derived
(`datasets.REGISTRY["kkbox-churn"].derived_by`). No network, no hosted call.

    uv run python scripts/build_kkbox_cohort.py            # the declared cut
    uv run python scripts/fetch_datasets.py --record       # then record its manifest entry

The cut is `kkbox.COHORT_USERS` users drawn by `kkbox.sample_users` (stratified on the label,
seeded). History is v1 `transactions.csv` plus the v2 rows up to the cutoff; nothing after
`kkbox.CUTOFF` is read.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from agentstack.context import datasets, kkbox

RAW = datasets.DATA_ROOT / "kkbox-raw"


def history(root: Path, users: set[str]) -> pd.DataFrame:
    """Transactions up to the cutoff for `users`, v1 and v2 together, duplicates dropped."""
    parts = [
        chunk[chunk.msno.isin(users) & (chunk.transaction_date <= kkbox.CUTOFF)]
        for chunk in pd.read_csv(root / "transactions.csv", chunksize=2_000_000)
    ]
    v2 = pd.read_csv(root / "transactions_v2.csv")
    v2 = v2[v2.msno.isin(users) & (v2.transaction_date <= kkbox.CUTOFF)]
    return pd.concat([*parts, v2], ignore_index=True).drop_duplicates()


def build(root: Path, users: int, seed: int, *, raw_dates: bool = False) -> pd.DataFrame:
    """The cohort frame: one row per sampled subscriber, `msno` as a column."""
    labels = pd.read_csv(root / "train_v2.csv")
    cut = kkbox.sample_users(labels, users, seed=seed)
    chosen = set(cut)
    members = pd.read_csv(root / "members_v3.csv")
    members = members[members.msno.isin(chosen)]
    registration = members.set_index("msno").registration_init_time.astype("float64")
    events = kkbox.to_events(history(root, chosen), registration, kkbox.CUTOFF)
    features = kkbox.to_features(
        events, members, labels[labels.msno.isin(chosen)], kkbox.CUTOFF, raw_dates=raw_dates
    )
    return features.reset_index()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=RAW)
    parser.add_argument("--out", type=Path, default=datasets.DATA_ROOT / "kkbox-cohort.csv")
    parser.add_argument("--users", type=int, default=kkbox.COHORT_USERS)
    parser.add_argument("--seed", type=int, default=kkbox.COHORT_SEED)
    args = parser.parse_args()

    frame = build(args.raw, args.users, args.seed)
    frame.to_csv(args.out, index=False)
    print(f"wrote {args.out}: {len(frame)} users, churn {frame[kkbox.TARGET].mean():.3f}")
    print("next: uv run python scripts/fetch_datasets.py --record")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
