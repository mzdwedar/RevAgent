"""Measure the cold-start claim: how good is each churn model with only n labelled subscribers?

    uv sync --extra prediction     # tabpfn brings scikit-learn and matplotlib
    uv run python scripts/cold_start_curve.py                 # score, then plot
    uv run python scripts/cold_start_curve.py --plot-only     # re-plot the committed CSV

The README argues TabPFN fits RevAgent because a new app has a few hundred labelled
subscribers, not years of them. This script turns that argument into a number. Per
dataset it holds out one fixed, stratified test set. It then draws training sets of n
rows from the rest, with several seeds per n, and scores the same test set with each
model:

- TabPFN-3.5, the same checkpoint and encoding `TabPFNScorer` uses, run locally;
- gradient-boosted trees with their defaults, the usual "just use boosted trees" answer.
  scikit-learn's histogram GBDT (LightGBM's algorithm) rather than LightGBM itself:
  LightGBM and torch each load their own OpenMP runtime on macOS, and in one process
  that segfaults;
- logistic regression, the floor.

Three numbers per fit:
- ROC-AUC: ranking quality, which is what targeting consumes.
- Brier score: calibration. Value at risk is computed from the probabilities.
- Churned revenue captured in the top decile: the share of the revenue that actually
  churned in the test set that lands in the 10% of customers the model ranks riskiest.
  This is the decile `experiments/targeting.toml` targets. It uses observed revenue
  only, so it is blank for bank-churn, which has none.

Always the local scorer: no row leaves the machine (ADR-0012 6). The output CSV holds
only aggregate metrics, no rows and no scores, so unlike `data/scores/` it can be
committed. Each fit is appended as it finishes and the run resumes from the CSV, so a
died run loses one fit, not the curve.
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import numpy as np
import pandas as pd

from agentstack.context import datasets
from agentstack.context.targeting import RevenueNotObserved, annual_revenue_cents
from agentstack.prediction.engine import CHECKPOINT, MODEL_VERSION, encode
from agentstack.prediction.licence import check_token, load_env

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "evidence" / "cold_start.csv"
FIGURE = ROOT / "docs" / "evidence" / "cold_start.png"

SIZES = (50, 100, 200, 500, 1000, 2000)
SEEDS = (0, 1, 2, 3, 4)
SPLIT_SEED = 20260922
TEST_ROWS = 2000
TOP_SHARE = 0.10
MODELS = ("tabpfn", "boosted-trees", "logistic")
FIELDS = (
    "dataset",
    "model",
    "n",
    "seed",
    "positives",
    "auc",
    "brier",
    "revenue_captured_top10",
    "seconds",
)


def stratified_take(labels: np.ndarray, rows: int, rng: np.random.Generator) -> np.ndarray:
    """`rows` positions with the population's churn rate, at least two of each class.

    At n=50 and a 9% churn rate, a plain random draw can land with no churner at all,
    and then no model can be fit. Two per class is the floor at which every model here
    is defined.
    """
    positives = np.flatnonzero(labels == 1)
    negatives = np.flatnonzero(labels == 0)
    want = min(len(positives), max(2, round(rows * len(positives) / len(labels))))
    chosen = np.concatenate(
        [
            rng.choice(positives, want, replace=False),
            rng.choice(negatives, min(len(negatives), rows - want), replace=False),
        ]
    )
    return np.sort(chosen)


def fit_predict(
    model: str, x_train: pd.DataFrame, y_train: np.ndarray, x_test: pd.DataFrame, seed: int
) -> np.ndarray:
    if model == "tabpfn":
        from tabpfn import TabPFNClassifier

        classifier = TabPFNClassifier.create_default_for_version(
            CHECKPOINT, random_state=seed, ignore_pretraining_limits=True
        )
    elif model == "boosted-trees":
        from sklearn.ensemble import HistGradientBoostingClassifier

        classifier = HistGradientBoostingClassifier(random_state=seed)
    elif model == "logistic":
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        classifier = make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(max_iter=2000)
        )
    else:
        raise ValueError(f"unknown model {model!r}")
    classifier.fit(x_train, y_train)
    return np.asarray(classifier.predict_proba(x_test))[:, 1]


def revenue_captured(scores: np.ndarray, churned: np.ndarray, revenue: np.ndarray | None) -> float:
    """The share of churned revenue that sits in the top `TOP_SHARE` by predicted risk."""
    if revenue is None:
        return float("nan")
    lost = revenue * churned
    take = int(np.ceil(len(scores) * TOP_SHARE))
    top = np.argsort(-scores, kind="stable")[:take]
    return float(lost[top].sum() / lost.sum())


def done_keys(path: Path) -> set[tuple[str, str, int, int]]:
    if not path.exists():
        return set()
    with path.open() as handle:
        return {
            (r["dataset"], r["model"], int(r["n"]), int(r["seed"])) for r in csv.DictReader(handle)
        }


def run(
    keys: list[str], sizes: tuple[int, ...], seeds: tuple[int, ...], models: tuple[str, ...]
) -> None:
    from sklearn.metrics import brier_score_loss, roc_auc_score

    if "tabpfn" in models:
        load_env()
        check_token()
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    finished = done_keys(RESULTS)
    fresh = not RESULTS.exists()

    with RESULTS.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if fresh:
            writer.writeheader()
        for key in keys:
            snapshot = datasets.load(key)
            target = datasets.REGISTRY[key].target
            features = encode(snapshot.frame.drop(columns=[target]))
            labels = snapshot.frame[target].to_numpy()
            try:
                revenue: np.ndarray | None = annual_revenue_cents(snapshot).to_numpy()
            except RevenueNotObserved:
                revenue = None

            rng = np.random.default_rng(SPLIT_SEED)
            test = stratified_take(labels, min(TEST_ROWS, len(labels) // 3), rng)
            pool = np.setdiff1d(np.arange(len(labels)), test)
            x_test, y_test = features.iloc[test], labels[test]
            test_revenue = None if revenue is None else revenue[test]
            print(
                f"----- {key}: {len(pool)} pool rows, {len(test)} test rows, "
                f"churn {labels.mean():.3f}"
            )

            for n in sizes:
                if n > len(pool):
                    continue
                for seed in seeds:
                    picked = pool[stratified_take(labels[pool], n, np.random.default_rng(seed))]
                    for model in models:
                        name = MODEL_VERSION if model == "tabpfn" else model
                        if (key, name, n, seed) in finished:
                            continue
                        started = time.perf_counter()
                        scores = fit_predict(
                            model, features.iloc[picked], labels[picked], x_test, seed
                        )
                        row = {
                            "dataset": key,
                            "model": name,
                            "n": n,
                            "seed": seed,
                            "positives": int(labels[picked].sum()),
                            "auc": round(float(roc_auc_score(y_test, scores)), 6),
                            "brier": round(float(brier_score_loss(y_test, scores)), 6),
                            "revenue_captured_top10": round(
                                revenue_captured(scores, y_test, test_revenue), 6
                            ),
                            "seconds": round(time.perf_counter() - started, 2),
                        }
                        writer.writerow(row)
                        handle.flush()
                        print(
                            f"  n={n:<5} seed={seed} {row['model']:<10} auc={row['auc']:.3f} "
                            f"brier={row['brier']:.3f} rev@10={row['revenue_captured_top10']:.3f} "
                            f"({row['seconds']}s)",
                            flush=True,
                        )


def plot() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    frame = pd.read_csv(RESULTS)
    metrics = [
        ("auc", "ROC-AUC (higher is better)"),
        ("revenue_captured_top10", "Churned revenue in top 10% (higher is better)"),
    ]
    keys = list(dict.fromkeys(frame["dataset"]))
    colours = {MODEL_VERSION: "#2563eb", "boosted-trees": "#d97706", "logistic": "#6b7280"}

    fig, axes = plt.subplots(len(metrics), len(keys), figsize=(4.2 * len(keys), 7), squeeze=False)
    for col, key in enumerate(keys):
        for row, (metric, label) in enumerate(metrics):
            ax = axes[row][col]
            subset = frame[frame["dataset"] == key]
            if subset[metric].isna().all():
                ax.text(
                    0.5,
                    0.5,
                    "no observed revenue",
                    ha="center",
                    va="center",
                    transform=ax.transAxes,
                    color="#6b7280",
                )
                ax.set_axis_off()
                continue
            for model, group in subset.groupby("model"):
                stats = group.groupby("n")[metric].agg(["mean", "std"]).reset_index()
                colour = colours.get(str(model), "#000000")
                ax.plot(
                    stats["n"],
                    stats["mean"],
                    marker="o",
                    label=model,
                    color=colour,
                    linewidth=2 if model == MODEL_VERSION else 1.3,
                )
                ax.fill_between(
                    stats["n"],
                    stats["mean"] - stats["std"],
                    stats["mean"] + stats["std"],
                    color=colour,
                    alpha=0.12,
                )
            ax.set_xscale("log")
            ax.set_xlabel("labelled subscribers (n)")
            ax.grid(alpha=0.3)
            if row == 0:
                ax.set_title(key)
            if col == 0:
                ax.set_ylabel(label)
    axes[0][0].legend(loc="lower right")
    fig.suptitle("Cold start: churn model quality vs labelled subscribers (mean ± sd over seeds)")
    fig.tight_layout()
    fig.savefig(FIGURE, dpi=150)
    print(f"wrote {FIGURE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--datasets", nargs="*", default=["telecom-bigml", "bank-churn", "kkbox-churn"]
    )
    parser.add_argument("--sizes", nargs="*", type=int, default=list(SIZES))
    parser.add_argument("--seeds", nargs="*", type=int, default=list(SEEDS))
    parser.add_argument("--models", nargs="*", default=list(MODELS), choices=MODELS)
    parser.add_argument("--plot-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.plot_only:
        run(args.datasets, tuple(args.sizes), tuple(args.seeds), tuple(args.models))
    plot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
