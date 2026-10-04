"""Can an LLM agent rank churn risk from the table itself, or does it need TabPFN?

    uv sync --extra prediction     # and `ollama pull qwen3:8b`
    uv run python scripts/llm_vs_tabpfn.py                # score, then plot
    uv run python scripts/llm_vs_tabpfn.py --plot-only    # re-plot the committed CSV

RevAgent's orchestrator is an LLM. The question this answers is whether that LLM could
do the targeting on its own, if it were given the subscriber table, or whether the
numbers have to come from a tabular model. Same rows, three arms:

- `llm`: the LLM reads n labelled subscribers as CSV and gives each test subscriber a
  churn risk from 0 to 100. This is what an agent does when it has no tabular tool.
- `tabpfn-3.5`: TabPFN-3.5 conditioned on the same n rows, the same encoding as
  `TabPFNScorer`.
- `llm+tabpfn`: the LLM sees the same table plus TabPFN's probability for each test
  subscriber, and gives the final risk. This is the agent with the tool. The question
  is whether it keeps the tool's ranking or talks itself out of it.

The splits are the cold-start ones (`cold_start_curve.SPLIT_SEED`), so the TabPFN arm
can be checked against `docs/evidence/cold_start.csv` at the same n.

An answer the LLM malforms (not JSON, a missing or extra id, a risk outside 0-100), or
one the Ollama runner dies giving, is retried once and then recorded as a failure with
no metrics. It is never repaired or
filled in, because a filled-in score would be ours, not the model's.

The model is local (`qwen3:8b` by default, RevAgent's own engine) at temperature 0, so
no row leaves the machine. It is an 8B model, and that is said wherever the numbers are.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
from cold_start_curve import SPLIT_SEED, TEST_ROWS, revenue_captured, stratified_take

from agentstack.context import datasets
from agentstack.context.targeting import RevenueNotObserved, annual_revenue_cents
from agentstack.model.contract import ModelRequest
from agentstack.model.ollama_engine import DEFAULT_MODEL, ModelUnavailable, OllamaEngine
from agentstack.prediction.engine import CHECKPOINT, MODEL_VERSION, encode
from agentstack.prediction.licence import check_token, load_env

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "evidence" / "llm_vs_tabpfn.csv"
FIGURE = ROOT / "docs" / "evidence" / "llm_vs_tabpfn.png"

DATASETS = ("kkbox-churn", "telecom-bigml")
TRAIN_ROWS = 200
TEST_SUBSET = 100
# Test subscribers per LLM call. The training table is the prompt's prefix and stays the
# same across a fit's chunks, so Ollama reuses its cache and only the chunk is new work.
CHUNK = 25
SEEDS = (0, 1, 2)
NUM_CTX = 16384  # the longest prompt (telecom, 25-row chunk) is ~8k tokens
# Ollama's runner can die mid-answer (seen: "unexpected EOF", status 500, under memory
# pressure on an 18 GB laptop). It restarts on the next request, so wait, then retry.
PAUSE_SECONDS = 10
ARMS = ("llm", "tabpfn", "llm+tabpfn")
FIELDS = (
    "dataset",
    "arm",
    "model",
    "n",
    "test_rows",
    "seed",
    "ok",
    "failure",
    "auc",
    "revenue_captured_top10",
    "seconds",
)

INSTRUCTIONS = """You are a churn analyst. You are given a table of past subscribers with a \
`churned` column (1 = churned, 0 = stayed), and then a table of new subscribers without it.

Estimate each new subscriber's churn risk as an integer from 0 (will certainly stay) to 100 \
(will certainly churn). Base it only on the patterns in the labelled table.{hint}

Reply with JSON only, no prose: {{"risks": [{{"id": <id>, "risk": <0-100>}}, ...]}} with \
exactly one entry for every id in the new-subscriber table."""

HINT = """ The new-subscriber table also has `model_churn_probability`, the output of a \
tabular churn model trained on the labelled table. Use it however you judge best."""


def table(frame: pd.DataFrame) -> str:
    """Rows as CSV, floats shortened: digits the model cannot use are tokens it pays for."""
    return frame.to_csv(index=False, float_format="%.4g")


def parse(text: str, ids: list[int]) -> list[float]:
    """The risks in `ids` order, or ValueError naming what was wrong."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ValueError("no JSON object in the reply")
    entries = json.loads(match.group(0))["risks"]
    risks: dict[int, float] = {}
    for entry in entries:
        risk = float(entry["risk"])
        if not 0 <= risk <= 100:
            raise ValueError(f"risk {risk} outside 0-100")
        risks[int(entry["id"])] = risk
    if sorted(risks) != sorted(ids):
        raise ValueError(f"ids {sorted(set(ids) ^ set(risks))[:5]} missing or extra")
    return [risks[i] / 100 for i in ids]


def ask_llm(
    engine: OllamaEngine, train: pd.DataFrame, test: pd.DataFrame, hint: bool
) -> list[float]:
    """Risk for every test row, asked `CHUNK` rows at a time, one retry per chunk."""
    instructions = INSTRUCTIONS.format(hint=HINT if hint else "")
    risks: list[float] = []
    for start in range(0, len(test), CHUNK):
        chunk = test.iloc[start : start + CHUNK]
        context = (
            f"Labelled subscribers ({len(train)} rows):\n{table(train)}\n"
            f"New subscribers ({len(chunk)} rows):\n{table(chunk)}"
        )
        request = ModelRequest(
            instructions=instructions,
            rendered_context=context,
            exposed_tools=(),
            max_output_tokens=60 + 20 * len(chunk),
        )
        ids = [int(i) for i in chunk["id"]]
        for attempt in (1, 2):
            try:
                risks.extend(parse(engine.generate(request).text, ids))
                break
            except ModelUnavailable as exc:
                if attempt == 2:
                    raise ValueError(f"rows {ids[0]}-{ids[-1]}: {exc}") from exc
                time.sleep(PAUSE_SECONDS)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                if attempt == 2:
                    raise ValueError(f"rows {ids[0]}-{ids[-1]}: {exc}") from exc
    return risks


def tabpfn_probabilities(
    x_train: pd.DataFrame, y_train: np.ndarray, x_test: pd.DataFrame, seed: int
) -> np.ndarray:
    from tabpfn import TabPFNClassifier

    classifier = TabPFNClassifier.create_default_for_version(
        CHECKPOINT, random_state=seed, ignore_pretraining_limits=True
    )
    classifier.fit(x_train, y_train)
    return np.asarray(classifier.predict_proba(x_test))[:, 1]


def done_keys(path: Path) -> set[tuple[str, str, int]]:
    if not path.exists():
        return set()
    with path.open() as handle:
        return {(r["dataset"], r["arm"], int(r["seed"])) for r in csv.DictReader(handle)}


def run(keys: list[str], seeds: tuple[int, ...], test_rows: int, model: str) -> None:
    from sklearn.metrics import roc_auc_score

    load_env()
    check_token()
    engine = OllamaEngine(model=model, num_ctx=NUM_CTX)
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
            raw = snapshot.frame.drop(columns=[target])
            encoded = encode(raw)
            labels = snapshot.frame[target].to_numpy()
            try:
                revenue: np.ndarray | None = annual_revenue_cents(snapshot).to_numpy()
            except RevenueNotObserved:
                revenue = None

            # The cold-start split: the same held-out set, the same training pool.
            test_all = stratified_take(
                labels, min(TEST_ROWS, len(labels) // 3), np.random.default_rng(SPLIT_SEED)
            )
            pool = np.setdiff1d(np.arange(len(labels)), test_all)
            print(f"----- {key}")

            for seed in seeds:
                rng = np.random.default_rng(seed)
                train = pool[stratified_take(labels[pool], TRAIN_ROWS, rng)]
                test = test_all[stratified_take(labels[test_all], test_rows, rng)]
                y_test = labels[test]
                test_revenue = None if revenue is None else revenue[test]

                train_table = raw.iloc[train].assign(churned=labels[train])
                test_table = raw.iloc[test].reset_index(drop=True)
                test_table.insert(0, "id", range(len(test_table)))
                if all((key, arm, seed) in finished for arm in ARMS):
                    continue
                # Once per seed: it is the TabPFN arm, and the tool output the third arm sees.
                probabilities = tabpfn_probabilities(
                    encoded.iloc[train], labels[train], encoded.iloc[test], seed
                )

                for arm in ARMS:
                    if (key, arm, seed) in finished:
                        continue
                    started = time.perf_counter()
                    failure = ""
                    scores: np.ndarray | None = None
                    try:
                        if arm == "tabpfn":
                            scores = probabilities
                        elif arm == "llm":
                            scores = np.array(ask_llm(engine, train_table, test_table, False))
                        else:
                            hinted = test_table.assign(
                                model_churn_probability=np.round(probabilities, 3)
                            )
                            scores = np.array(ask_llm(engine, train_table, hinted, True))
                    except ValueError as exc:
                        failure = str(exc)[:200]
                    row = {
                        "dataset": key,
                        "arm": arm,
                        "model": MODEL_VERSION if arm == "tabpfn" else model,
                        "n": TRAIN_ROWS,
                        "test_rows": len(test),
                        "seed": seed,
                        "ok": int(scores is not None),
                        "failure": failure,
                        "auc": ""
                        if scores is None
                        else round(float(roc_auc_score(y_test, scores)), 6),
                        "revenue_captured_top10": ""
                        if scores is None
                        else round(revenue_captured(scores, y_test, test_revenue), 6),
                        "seconds": round(time.perf_counter() - started, 1),
                    }
                    writer.writerow(row)
                    handle.flush()
                    print(
                        f"  seed={seed} {arm:<11} auc={row['auc']} "
                        f"rev@10={row['revenue_captured_top10']} ({row['seconds']}s) {failure}",
                        flush=True,
                    )


def plot() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    frame = pd.read_csv(RESULTS)
    ok = frame[frame["ok"] == 1]
    keys = list(dict.fromkeys(frame["dataset"]))
    labels = {"llm": "LLM alone", "tabpfn": "TabPFN-3.5", "llm+tabpfn": "LLM + TabPFN tool"}
    colours = {"llm": "#6b7280", "tabpfn": "#2563eb", "llm+tabpfn": "#7c3aed"}
    metrics = [("auc", "ROC-AUC"), ("revenue_captured_top10", "Churned revenue in top 10%")]

    fig, axes = plt.subplots(1, len(metrics), figsize=(5.2 * len(metrics), 4), squeeze=False)
    width = 0.8 / len(ARMS)
    for col, (metric, title) in enumerate(metrics):
        ax = axes[0][col]
        for i, arm in enumerate(ARMS):
            stats = ok[ok["arm"] == arm].groupby("dataset")[metric].agg(["mean", "std"])
            stats = stats.reindex(keys)
            x = np.arange(len(keys)) + (i - (len(ARMS) - 1) / 2) * width
            ax.bar(
                x,
                stats["mean"],
                width,
                yerr=stats["std"],
                label=labels[arm],
                color=colours[arm],
                capsize=3,
            )
        ax.set_xticks(np.arange(len(keys)), keys)
        ax.set_title(title)
        ax.set_ylim(0, 1)
        ax.grid(axis="y", alpha=0.3)
    axes[0][0].legend(loc="lower left")
    failures = int((frame["ok"] == 0).sum())
    model = next((m for m in frame["model"] if m != MODEL_VERSION), "LLM")
    fig.suptitle(
        f"{model} vs TabPFN-3.5 on the same {TRAIN_ROWS} labelled subscribers "
        f"(mean ± sd over seeds; {failures} malformed LLM answers excluded)"
    )
    fig.tight_layout()
    fig.savefig(FIGURE, dpi=150)
    print(f"wrote {FIGURE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--datasets", nargs="*", default=list(DATASETS))
    parser.add_argument("--seeds", nargs="*", type=int, default=list(SEEDS))
    parser.add_argument("--test-rows", type=int, default=TEST_SUBSET)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--plot-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.plot_only:
        run(args.datasets, tuple(args.seeds), args.test_rows, args.model)
    plot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
