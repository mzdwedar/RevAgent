# RevAgent

RevAgent runs churn-prevention experiments for a subscription business. It spots when
retention is slipping, picks the subscribers worth acting on, drafts an experiment, and
rolls it out only after a person approves it.

## Contents
- [The result, in one page](#the-result-in-one-page)
- [Reproducibility](#reproducibility)
- [What it does](#what-it-does)
- [The business problem](#the-business-problem)
- [Why PriorLabs' TabPFN fits this use case](#why-priorlabs-tabpfn-fits-this-use-case)
- [Licensing, before anything else](#licensing-before-anything-else)
- [Quickstart](#quickstart)
- [Datasets](#datasets)
- [Configuration](#configuration)
- [Commands](#commands)
- [The gates](#the-gates)
  - [CI](#ci)
- [Layout](#layout)
- [Workflow](#workflow)
- [Documentation](#documentation)
- [Local vs production](#local-vs-production)
- [Gaps before production](#gaps-before-production)
- [Contributing and security](#contributing-and-security)
- [What is deliberately unfinished](#what-is-deliberately-unfinished)
- [References](#references)

## The result, in one page

**Thesis: LLM agents are bad with tables. TabPFN-3.5 is their tabular brain.**

RevAgent is built for an indie developer with 200 subscribers: too few rows to train a
churn model, too many to eyeball. It uses a local LLM (qwen3:8b) to run the workflow and
TabPFN-3.5, run locally, to read the table. Nothing reaches a customer until a person
approves that exact rollout.

### Proof 1: the LLM cannot rank churn from the table, TabPFN can

Same 200 labelled KKBox subscribers, same 100 held-out test rows, three seeds
([`scripts/llm_vs_tabpfn.py`](scripts/llm_vs_tabpfn.py), data in
[`docs/evidence/llm_vs_tabpfn.csv`](docs/evidence/llm_vs_tabpfn.csv)).

| Arm | AUC (mean of 3 seeds) | Revenue captured in top 10% |
|---|---|---|
| qwen3:8b alone, shown the table | **0.535** | 0.101 |
| TabPFN-3.5 | **0.842** | 0.477 |
| qwen3:8b given TabPFN's score | 0.772 | 0.477 |

![LLM vs TabPFN on KKBox](docs/evidence/llm_vs_tabpfn.png)

What this does and does not show:

- The LLM alone is close to a coin flip (AUC 0.45 to 0.61 by seed). TabPFN is 0.79 to 0.88.
- The third arm is weaker than we hoped. Handing the LLM the TabPFN score did **not** restore
  TabPFN's AUC: it is 0.87, 0.85 and 0.60 across the seeds, and seed 2 is a real failure of
  the LLM to use the score. The revenue captured in the top 10% does match TabPFN's on every
  seed. We claim "the LLM cannot rank churn from the table and TabPFN can", not "the LLM
  plus TabPFN is as good as TabPFN".
- Caveats: **KKBox only, 3 seeds, 100 test rows.** Revenue is observed (what the subscriber
  last paid, annualised by the length of the plan it bought, in NTD), never predicted. The
  revenue column was corrected during this work (it was last payment x 12, about 2.5x too
  high) and every revenue figure here was re-run on the corrected basis. In two of three
  seeds the LLM alone captured no churned revenue in its top 10%.

### Why TabPFN: the cold-start curve

AUC by number of labelled training rows, mean of 5 seeds
([`scripts/cold_start_curve.py`](scripts/cold_start_curve.py),
[`docs/evidence/cold_start.csv`](docs/evidence/cold_start.csv)). Rows below are n=200, the
indie-developer case; the full curve runs 50 to 2,000.

| Dataset | TabPFN-3.5 | Boosted trees | Logistic |
|---|---|---|---|
| KKBox | **0.844** | 0.733 | 0.746 |
| telecom-bigml | **0.876** | 0.704 | 0.747 |
| bank-churn | **0.814** | 0.694 | 0.684 |
| IBM Telco | **0.822** | 0.784 | 0.809 |
| Netflix (synthetic) | 0.972 | 0.974 | 0.950 |

![Cold-start curve](docs/evidence/cold_start.png)

TabPFN leads at every size on KKBox, telecom and bank, and the gap closes as rows grow (on
telecom, boosted trees catch up by about 1,000 rows). On IBM Telco the lead is small. On
Netflix there is no lead: the data is easy (AUC 0.97 to 1.0 for every model), so we make **no
TabPFN claim on Netflix**.

## Reproducibility

Both results above come from scripts in `scripts/`. They run locally: TabPFN-3.5 on this
machine, the LLM through Ollama. No row leaves the machine.

**Once:**

```bash
uv sync --extra prediction                      # TabPFN (pulls torch)
export TABPFN_TOKEN="<your-api-key>"            # see Licensing below
brew install ollama && brew services start ollama
ollama pull qwen3:8b
```

**Data** (needs `~/.kaggle/kaggle.json`, and the KKBox competition rules accepted once):

```bash
uv run python scripts/fetch_datasets.py          # every fetch-only dataset
uv run python scripts/fetch_datasets.py --kkbox  # KKBox raw files
uv run python scripts/build_kkbox_cohort.py      # derive data/kkbox-cohort.csv
```

**Proof 1, LLM vs TabPFN, on all three datasets:**

```bash
uv run python scripts/llm_vs_tabpfn.py --datasets kkbox-churn telecom-bigml bank-churn
```

With no `--datasets` it runs `kkbox-churn` and `telecom-bigml`. The committed
[`docs/evidence/llm_vs_tabpfn.csv`](docs/evidence/llm_vs_tabpfn.csv) holds KKBox only.
The script appends to that CSV and skips any (dataset, arm, seed) already in it, so a run
fills in the missing datasets and keeps the KKBox rows. Delete the CSV to re-run
everything from scratch. `--seeds` and `--test-rows` change the protocol (defaults: seeds
0 1 2, 100 test rows), and `--plot-only` redraws the figure from the CSV.

**The cold-start curve:**

```bash
uv run python scripts/cold_start_curve.py              # score, then plot
uv run python scripts/cold_start_curve.py --plot-only  # redraw from the committed CSV
```

Its default is `telecom-bigml`, `bank-churn` and `kkbox-churn`. For the IBM Telco and
Netflix rows of the table, add them:
`--datasets telecom-bigml bank-churn kkbox-churn ibm-telco netflix-churn`.

## What it does

The agent is an **Experiment Operator** for a subscription business. It
runs churn-prevention experiments:

1. A trigger (a metric movement) starts a run.
2. TabPFN scores churn risk and a targeting step picks the cohort.
3. A policy check produces an experiment draft in the registry.
4. A human approves it in Slack, bound to that exact action.
5. The rollout (for example 10% to a variant) commits exactly once.

The rollout is the only externally visible act. The primary metric is Incremental Net
Saved Value, with conversion and churn as guardrails. Full spec: [`SPEC.md`](SPEC.md).

```
trigger -> run (Temporal) -> context -> churn score -> policy -> approval (Slack) -> gateway -> rollout
              |                |            |             |            |               |
           Postgres      scoped, fresh   TabPFN      decision +    bound to run id   idempotent,
           record        provenance                  envelope      + fingerprint     one commit
```

The eleven packages under `src/agentstack/` are organised into ten layers:

| # | Layer | Owner module |
|---|---|---|
| 1 | Interfaces & channels | `agentstack.interfaces` |
| 2 | Control plane & session ownership | `agentstack.control_plane` |
| 3 | Runtime, workflows, durable execution | `agentstack.runtime` |
| 4 | Model engine & inference | `agentstack.model` |
| 4b | Tabular inference (churn) | `agentstack.prediction` |
| 5 | Context, retrieval, memory | `agentstack.context` |
| 6 | Tools, MCP, capability surfaces | `agentstack.tools` |
| 7 | Execution surfaces | `agentstack.execution` |
| 8 | Identity, trust, policy, approvals | `agentstack.policy` |
| 9 | Observability, evaluation, feedback | `agentstack.observability` |
| 10 | Infrastructure substrate | `agentstack.storage` |

The rules each layer must keep live in [`STACK.md`](STACK.md).

## The business problem

Subscription revenue leaks through churn, and the usual fixes (a discount, an extended
trial, a win-back offer) cost money. Three things go wrong when teams run them by hand:

- **Offers go to the wrong people.** A discount given to someone who would have stayed
  anyway is pure margin loss. The question is who is *persuadable*, which is why the
  primary metric is Incremental Net Saved Value rather than raw retention.
- **Experiments are slow and noisy.** Picking cohorts, drafting variants and reading
  results is manual work, and noisy metrics hide real effects.
- **A mistake is expensive and visible.** A rollout that fires twice, fires without sign-off,
  or acts on stale data hits real customers and real revenue.

RevAgent takes the repetitive work (detect, score, target, draft) and leaves the
consequential decision to a person: nothing reaches customers until someone approves
that exact rollout in Slack, and it then commits exactly once. Every run leaves an audit
trail, and every failure that reaches production becomes a regression case in `evals/`.

How the code is organised to deliver that is in the table above and in
[`STACK.md`](STACK.md).

## Why PriorLabs' TabPFN fits this use case

RevAgent serves many apps, and most of them are small or new. A new app has a few
hundred subscribers and only a few weeks of churn labels; a mature one has years. A
churn model that has to be trained per app fails exactly where the agent is asked to act
first. TabPFN is a tabular foundation model, pretrained on a large number of synthetic
tabular tasks, and that changes the picture:

- **It works with very little data.** Prediction is in-context: the app's labelled
  subscribers are passed alongside the ones to score, and there is no per-app training
  run. Small datasets are the case it was built for, where gradient-boosted trees
  tend to overfit or need tuning.
- **No cold-start pipeline per app.** There is no feature-selection pass, hyperparameter
  search or retraining schedule to onboard a new app. The same checkpoint scores a
  telecom, bank or KKBox-shaped cohort, which is what the datasets in this repo exercise.
- **It copes with messy subscription tables.** Mixed numeric and categorical columns and
  missing values go in as they are, so the feature mapping stays thin.
- **It returns probabilities.** The targeting step needs a churn probability to rank and
  threshold on, and the Incremental Net Saved Value maths needs that probability to be
  usable, not just a class label.
- **Provenance is simple.** One named checkpoint scores every run, and it is recorded
  in each experiment's `experiment_version` (see [Licensing](#licensing-before-anything-else)).
  There is no per-app model artefact to version, store or go stale.

## Licensing, before anything else

**The churn model at the centre of this system is licensed for non-commercial use.**

The code in this repository is licensed under the [Apache License 2.0](LICENSE). The TabPFN-3.5 weights it calls are a separate asset under Prior Labs' own non-commercial licence, which the code does not relicense. That constraint is below.

The dependency's terms are settled:
`agentstack.prediction` scores churn with PriorLabs' **TabPFN-3.5**, whose weights are
open for **non-commercial use only**. Using them means accepting that licence once per
machine, through a gated Hugging Face repository:

1. Register at <https://ux.priorlabs.ai> and accept the licence on the **Licenses** tab
2. Copy the API key from <https://ux.priorlabs.ai/account>
3. `export TABPFN_TOKEN="<your-api-key>"`

**What this means in practice.** Everything here is shaped like a production system —
durable runtime, multi-tenant, side-effecting, approval-gated — and the model at the
centre of it cannot be shipped in a commercial product under this licence. That is a
real constraint, not a setup step, and it is stated here rather than discovered during
a launch review.

If you need a commercial path, the checkpoint is one constant:
`CHECKPOINT` in `src/agentstack/prediction/engine.py`. `tabpfn` 9.x gates v2.5, v2.6,
v3, v3.5 and v3.5-fast; **v2 is ungated** and needs no acceptance step at all. Whether
v2's own terms suit your use is a question for its licence — this note records only
that it does not require the gate above.

The checkpoint is named explicitly rather than inherited from the package default,
because the model version travels in every experiment's `experiment_version` and a
silent upgrade would make recorded provenance wrong.

## Quickstart

**Prerequisites:** Python 3.12, [`uv`](https://docs.astral.sh/uv/), Docker, and (for the
full workflow) Ollama and Kaggle credentials.

```bash
uv sync
bash scripts/dev_up.sh        # Postgres 16 + Temporal dev server, then migrations
uv run agentstack
```

Recorded TabPFN scores for the three local datasets live in `data/scores/`; to regenerate
them, set `TABPFN_TOKEN` and run `uv run python scripts/record_scores.py` (needs
`uv sync --extra prediction`).

`dev_up.sh` runs `docker compose up --wait`, applies migrations and sets Temporal
namespace retention to 168h. Local ports:

| Service | Address |
|---|---|
| Postgres | `localhost:5433` (user `agent`, password `agent`, db `agentstack`) |
| Temporal | `localhost:7233` (UI on `localhost:8233`) |

The walkthrough above needs only the substrate. The full workflow also wants a model
engine and the cohort data:

```bash
brew install ollama && brew services start ollama
ollama pull qwen3:8b                      # dev only; production runs on a cloud GPU
uv run python scripts/fetch_datasets.py   # needs ~/.kaggle/kaggle.json
uv sync --extra prediction                # optional: TabPFN (pulls torch); needs TABPFN_TOKEN
```

One rollout walks the whole stack: context assembled and fingerprinted, tools exposed
for this run only, a policy decision, a run parked on a human approval bound to that
exact action, a resume against the same run id, the commit — and then two retries that
are deduplicated instead of rolling out three times.

To run the long-lived processes, start the worker and (for approvals) the Slack receiver:

```bash
uv run --env-file .env agentstack-worker
uv run --env-file .env agentstack-slack
```

## Datasets

Churn datasets stand in for a subscription business's customer base. Only one is committed;
the rest are third-party and fetch-only.

| Key | Source | Rows | Churn rate | Committed? |
|---|---|---|---|---|
| `netflix-churn` | [Kaggle `zeyadmohamed26/netflix-customer-churn-and-engagement-analytics`](https://www.kaggle.com/datasets/zeyadmohamed26/netflix-customer-churn-and-engagement-analytics), CC0 1.0 | 5,000 | 50% | yes, `data/open/` (probably synthetic) |
| `kkbox-churn` | [KKBox churn prediction challenge](https://www.kaggle.com/competitions/kkbox-churn-prediction-challenge/data) (Kaggle competition; accept its rules once) | 49,863 | 8.9% | no, fetch only |
| `telecom-bigml` | [`mnassrib/telecom-churn-datasets`](https://www.kaggle.com/datasets/mnassrib/telecom-churn-datasets) | 3,333 | 14.5% | no, fetch only |
| `bank-churn` | [`radheshyamkollipara/bank-customer-churn`](https://www.kaggle.com/datasets/radheshyamkollipara/bank-customer-churn) | 10,000 | 20.4% | no, fetch only |
| `ibm-telco` | [`blastchar/telco-customer-churn`](https://www.kaggle.com/datasets/blastchar/telco-customer-churn) (IBM sample data) | 7,043 | 26.5% | no, fetch only |

None is subscription-app data except the Netflix file, which is synthetic as far as we can
tell, so read results on it as pipeline demonstration. In `bank-churn` the `Complain` column
is dropped: it correlates with the target at r=0.996 because the complaint is logged as part
of the churn event. Specs, dropped columns and revenue columns are in
`src/agentstack/context/datasets.py`.

**Licences.** The code is under the [Apache License 2.0](LICENSE). Data keeps
its own licence: the committed Netflix file is CC0 ([`data/open/NOTICE.md`](data/open/NOTICE.md)),
and everything else is licensed for use but not redistribution, so it is gitignored. Fetch it
with `uv run python scripts/fetch_datasets.py` (needs `~/.kaggle/kaggle.json`). The committed
`data/manifest.json` records row counts, columns and a `data_as_of` hash, so an upstream change
shows up as a diff. Check each dataset's licence before reuse.

## Configuration

Set these in the environment, or in a gitignored `.env` loaded with
`uv run --env-file .env`. [`.env.example`](.env.example) lists the names.

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | Postgres connection string | `postgresql://agent:agent@localhost:5433/agentstack` |
| `TEMPORAL_ADDRESS` | Temporal frontend | `localhost:7233` |
| `TABPFN_TOKEN` | PriorLabs key for TabPFN (see Licensing) | none |
| `SLACK_BOT_TOKEN` | Slack bot token for approval messages | none |
| `SLACK_SIGNING_SECRET` | verifies inbound Slack requests | none |

Tests and CI also read `TEMPORAL_TEST_SERVER_DIR` and `AGENTSTACK_RECORD_HISTORIES`.
Experiment thresholds are versioned in `experiments/targeting.toml` and
`experiments/cadence.toml`: change a number there, not in code.

## Commands

| Command | Purpose |
|---|---|
| `agentstack` | the CLI interface |
| `agentstack-migrate` | schema migrations (`up`, `status`) |
| `agentstack-preflight` | the licence gate, as a deploy would run it |
| `agentstack-operator` | inspect runs (`status`, `stalled --older-than 7d`) and settle an unresolved effect (`reconcile`) |
| `agentstack-worker` | the Temporal worker |
| `agentstack-slack` | the Slack Bolt HTTP receiver ([ADR-0011](docs/adr/0011-slack-bolt-receiver.md)) |

## The gates

| Command | What it checks | Budget | When |
|---|---|---|---|
| `bash scripts/check_fast.sh` | lint, format, types, layer contracts, capability metadata | < 5s | every edit (PostToolUse hook) |
| `bash scripts/check_task.sh` | the above + full suite + changed-line coverage + bar integrity | < 90s | turn end (Stop hook) |
| `bash scripts/check_full.sh` | the above + release gates + secrets + dependencies | minutes | CI |
| `uv run python scripts/stack_guard.py --base main` | did the bar itself get weakened? | seconds | task end, CI |
| `uv run python -m evals run --gates` | the Observability, evaluation, feedback release gates | seconds | CI |

The hooks in `.claude/settings.json` run the first two automatically and block on
failure, so the loop fails closed whether or not anyone remembers to run them.

Other suites: `uv run pytest tests/fitness -v` (the architecture bar),
`uv run pytest tests/live` (real datasets and model; excluded by default), and
`uv run python scripts/checkpoint_guard.py --base main` (would this deploy strand live
runs?). `tests/infra` and `tests/durability` **fail** rather than skip when Postgres or
Temporal is missing. Coverage floors (changed-line and project) are set in
`CONSTRAINTS.md`.

### CI

`.github/workflows/ci.yml` runs one `bar` job on pull requests and pushes to `main`:
Postgres and Temporal, `uv sync --frozen`, `check_fast`, pytest with coverage,
changed-line coverage (PRs), the eval gates, `stack_guard` (PRs), `checkpoint_guard`,
gitleaks and osv-scanner.

## Layout

| Path | Purpose |
|---|---|
| `STACK.md` | the layer ledger: owner module, store, invariants, status |
| `CONSTRAINTS.md` | the bar, with numbers and the command that produces each verdict |
| `CLAUDE.md` / `AGENTS.md` | what an agent must read before writing code here |
| `docs/spec-template.md` | six core areas plus the three Agent Stack sections |
| `docs/adr/` | one ADR per boundary decision |
| `src/agentstack/` | 11 packages across the ten layers, dependency direction enforced by six `.importlinter` contracts |
| `migrations/` | versioned SQL; an applied migration is immutable, a version gap is refused |
| `experiments/` | targeting thresholds, versioned — change a number here, not in code |
| `SPEC*.md` | the Experiment Operator spec, the durable runtime and the registry |
| `tests/fitness/` | 52 tests, one per collapsed-boundary failure mode |
| `tests/live/` | checks needing real datasets or the model; excluded from CI, declared in `CONSTRAINTS.md` |
| `tests/durability/` | spawns real worker processes, kills them with SIGKILL, and a fresh one resumes the run from Temporal's history and the Postgres record |
| `evals/` | 13 release gates that judge the path, not just the answer |
| `scripts/` | the three check stages, the bar guard, and the checkpoint and replay guards |
| `checkpoints/` | checkpoint schema, so a deploy cannot strand live runs |
| `docs/evidence/` | Measured results of the Cedar vs Rego and Temporal probes (code removed; see its READMEs) |
| `data/` | cohort datasets (gitignored) and the committed `manifest.json` |
| `.claude/` | hooks, the `agent-stack-auditor` subagent, and `/spec` `/plan` `/stack-audit` |

## Workflow

`/spec` → `/plan` → `/build` → `/review` → `/stack-audit` → `/ship`.

`/spec` and `/plan` are extended locally: a spec must carry the Layer Ownership
Ledger, Foundation Assumptions and Boundary Decisions; a task must name its layer and
the test that proves it. `/stack-audit` is mandatory before `/ship` and covers the
architectural judgement a test cannot make — tool granularity, approval legibility,
whether a memory should exist at all.

Non-negotiables, in short: every side effect goes through
`agentstack.execution.gateway.execute` with an identity envelope and idempotency key;
approval sits immediately before the irreversible act; the database driver is imported
only in `agentstack.storage`; applied migrations are never edited; untrusted content never
influences an authority decision. The full list is in [`CLAUDE.md`](CLAUDE.md).

## Documentation

| Doc | Contents |
|---|---|
| [`STACK.md`](STACK.md) | layer ledger: owners, stores, rules and status |
| [`CONSTRAINTS.md`](CONSTRAINTS.md) | the quality bar and its amendment log |
| [`SPEC.md`](SPEC.md), [`SPEC-durable-runtime.md`](SPEC-durable-runtime.md), [`SPEC-registry.md`](SPEC-registry.md) | what is being built |
| [`docs/adr/`](docs/adr/) | ADRs 0001-0011 (architecture, durable backend, model contract, egress, migrations, idempotency, Temporal hosting, Slack receiver) |
| [`tasks/plan.md`](tasks/plan.md), [`tasks/todo.md`](tasks/todo.md) | the plan and checklist |
| [`migrations/README.md`](migrations/README.md), [`checkpoints/README.md`](checkpoints/README.md) | schema and checkpoint rules |

## Local vs production

Each component as it runs today (grey, left) and what it becomes in production (right).
Colour says how far the production side is from existing; details are in
[Gaps before production](#gaps-before-production).

```mermaid
flowchart LR
  HL["<b>Today</b> (local)"]:::head ~~~ HP["<b>Production</b>"]:::head
  L1["Trigger<br/>injected by hand via agentstack-slack"] --> P1["Event ingress<br/>from subscription-platform webhooks / metrics"]
  L2["Cohort data<br/>Kaggle snapshots in data/"] --> P2["Subscriber data<br/>live subscription events, with a watermark"]
  L3["Temporal<br/>dev server, temporal:1.9.1"] --> P3["Temporal Cloud or self-hosted<br/>ADR-0009"]
  L4["Postgres 16<br/>container on :5433"] --> P4["Managed Postgres<br/>backups, ADR-0005 migrations"]
  L5["Worker + Slack receiver<br/>uv run on a laptop"] --> P5["Container images<br/>public HTTPS for Slack"]
  L6["LLM<br/>Ollama qwen3:8b on :11434"] --> P6["GPU serving stack<br/>tool calls verified equivalent"]
  L7["TabPFN-3.5<br/>in-process, or recorded scores"] --> P7["TabPFN on GPU or a service<br/>commercial licence"]
  L8["Rollout<br/>Postgres registry + RecordingClient fake"] --> P8["Real rollout client<br/>offerings / paywall / flags"]
  L9["Secrets<br/>.env through storage/secrets.py"] --> P9["Secret store<br/>with rotation"]
  L10["Traces<br/>LoggingSink to Python logging"] --> P10["Trace backend + alerts<br/>retention decided"]
  classDef head fill:none,stroke:none
  class L1,L2,L3,L4,L5,L6,L7,L8,L9,L10 now
  classDef now fill:#f4f4f4,stroke:#666,color:#000
  classDef blocker fill:#fde2e1,stroke:#c0392b,color:#000
  classDef decided fill:#fff4d6,stroke:#b7791f,color:#000
  classDef open fill:#e3ecfa,stroke:#2b6cb0,color:#000
  class P1,P2,P7,P8 blocker
  class P3,P4,P9 decided
  class P5,P6,P10 open
```

- **Red:** blocks production. Nothing exists yet, or the licence forbids it.
- **Amber:** the invariants are fixed in an ADR or a seam, and only the host or vendor is
  still to choose.
- **Blue:** open, with no decision recorded.

`docker-compose.yml` is for development only. Before any deploy, run
`agentstack-preflight` (licence gate) and `checkpoint_guard` (no stranded runs).

## Gaps before production

The workflow runs end to end locally, against public datasets, with a person approving
in Slack. That is not the same as running it for a real subscription business. Each gap
below is either an open decision in a spec or ADR, or a reference stand-in in the code.

| Gap | Where it stands | What closing it needs |
|---|---|---|
| **Model licence** | TabPFN-3.5 weights are non-commercial ([Licensing](#licensing-before-anything-else)) | a commercial licence from Prior Labs, or a different checkpoint whose terms allow it |
| **Subscriber data** | cohorts are Kaggle snapshots on disk (`context/datasets.py`, `data/`); KKBox is mapped to a subscription-event schema (ADR-0012) but is not live data | ingestion from the subscription platform's webhooks or exports, its cadence and watermark (`SPEC.md`, open question) |
| **Trigger source** | triggers are injected by hand; the only caller of `deliver` is `agentstack-slack` (`slack_cli.py`), and nothing detects a metric movement | a scheduled or event-driven ingress that computes the metric and emits `data_arrival` / `metric_movement` with a watermark |
| **The rollout target** | a rollout commits to our own Postgres registry; the external API client is `RecordingClient`, a reference fake (`interfaces/wiring.py`) | a real client for wherever variants are served (offerings, paywall or flag service), behind the gateway, plus its credential and identity model (iteration 2) |
| **Measuring the effect** | Incremental Net Saved Value, ANCOVA and the regressor are designed but not built (`SPEC.md`, iteration 2) | the readout, so an experiment can be concluded rather than only rolled out |
| **LLM serving** | Ollama `qwen3:8b`, host defaults to `localhost:11434` (`model/ollama_engine.py`) | a GPU serving stack (open), verified tool-call equivalence with dev, and a per-evaluation cost budget |
| **TabPFN serving** | runs as an in-process library; most recorded scores exist only on the machine that made them | GPU capacity or a scoring service behind the `prediction/` seam, and scores reproducible outside one laptop |
| **Hosting** | `docker-compose.yml` is dev only; no container image or deploy manifest exists; Temporal hosting is deferred ([ADR-0009](docs/adr/0009-temporal-production-hosting.md)) | images for the worker and the Slack receiver, managed Postgres with backups, and the Temporal decision (Cloud needs a payload codec) |
| **Secrets** | read through one seam (`storage/secrets.py`), but the source is still the process environment ([ADR-0013](docs/adr/0013-secrets-seam-and-span-allowlist.md)) | a secret store, and rotation that does not strand approvals already waiting |
| **Observability** | spans go to `LoggingSink` (Python logging), with an attribute allowlist; no metrics or alerts | a trace backend, alerts on stalled runs and unresolved effects, and a decided retention period for traces, transcripts and audit rows |
| **PII** | the span allowlist keeps rows out of traces; prose redaction is a net, and transcripts and audit rows are untouched | a retention and deletion job, and a review of what transcripts hold once real subscriber data lands |

The first four are blockers: without them there is nothing legal to run, no real data to run
on, nothing to start a run, and no customer-visible effect. The rest are what makes it safe
to leave running.

## Contributing and security

There is no `CONTRIBUTING.md`, `SECURITY.md` or `CODEOWNERS` yet. Contributions are accepted under the repository licence. Until there is, the
contributor rules are `CLAUDE.md` and `AGENTS.md`, and every change must pass
`check_task.sh` and, before shipping, `/stack-audit`. Never commit `.env`.

## What is deliberately unfinished

- **Recorded TabPFN scores for third-party datasets stay local.** Only the Netflix
  scores (CC0 data) are committed, and a fitness test holds them to their dataset hash.
  KKBox, telecom and bank scores are excluded by `.gitignore` on purpose: they are scores
  of licensed rows. So CI and a fresh clone replay Netflix only, and any other dataset
  needs `scripts/record_scores.py`, which needs the licence above and `TABPFN_TOKEN`.

## References

- [The Agent Stack](https://theagentstack.substack.com/): the ten-layer model and the six
  boundary confusions this repository is built to.
- [Variance reduction](https://confidence.spotify.com/docs/experiments/stats/variance-reduction)
  (Spotify Confidence docs): the statistical background for adjusting experiment metrics
  with pre-experiment covariates, which is the idea behind using predicted churn to sharpen
  the experiment readout.
