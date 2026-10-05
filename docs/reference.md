# RevAgent reference

The operating detail behind the [README](../README.md): datasets, configuration,
commands, the quality gates, the repository layout, and where each document lives.

## Contents
- [Datasets](#datasets)
- [Configuration](#configuration)
- [Commands](#commands)
- [The gates](#the-gates)
  - [CI](#ci)
- [Layout](#layout)
- [Workflow](#workflow)
- [Documentation](#documentation)
- [Contributing and security](#contributing-and-security)
- [What is deliberately unfinished](#what-is-deliberately-unfinished)

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

**Licences.** The code is under the [Apache License 2.0](../LICENSE). Data keeps
its own licence: the committed Netflix file is CC0 ([`data/open/NOTICE.md`](../data/open/NOTICE.md)),
and everything else is licensed for use but not redistribution, so it is gitignored. Fetch it
with `uv run python scripts/fetch_datasets.py` (needs `~/.kaggle/kaggle.json`). The committed
`data/manifest.json` records row counts, columns and a `data_as_of` hash, so an upstream change
shows up as a diff. Check each dataset's licence before reuse.

## Configuration

Set these in the environment, or in a gitignored `.env` loaded with
`uv run --env-file .env`. [`.env.example`](../.env.example) lists the names.

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | Postgres connection string | `postgresql://agent:agent@localhost:5433/agentstack` |
| `TEMPORAL_ADDRESS` | Temporal frontend | `localhost:7233` |
| `TABPFN_TOKEN` | PriorLabs key for TabPFN (see Licensing in the README) | none |
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
| `agentstack-slack` | the Slack Bolt HTTP receiver ([ADR-0011](adr/0011-slack-bolt-receiver.md)) |

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
| `evals/` | 20 release gates that judge the path, not just the answer |
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
influences an authority decision. The full list is in [`CLAUDE.md`](../CLAUDE.md).

## Documentation

| Doc | Contents |
|---|---|
| [`STACK.md`](../STACK.md) | layer ledger: owners, stores, rules and status |
| [`CONSTRAINTS.md`](../CONSTRAINTS.md) | the quality bar and its amendment log |
| [`SPEC.md`](../SPEC.md), [`SPEC-durable-runtime.md`](../SPEC-durable-runtime.md), [`SPEC-registry.md`](../SPEC-registry.md) | what is being built |
| [`docs/adr/`](adr/) | ADRs 0001-0013 (architecture, durable backend, model contract, egress, migrations, idempotency, Temporal hosting, Slack receiver, KKBox cohort, secrets) |
| [`tasks/plan.md`](../tasks/plan.md), [`tasks/todo.md`](../tasks/todo.md) | the plan and checklist |
| [`migrations/README.md`](../migrations/README.md), [`checkpoints/README.md`](../checkpoints/README.md) | schema and checkpoint rules |

## Contributing and security

There is no `CONTRIBUTING.md`, `SECURITY.md` or `CODEOWNERS` yet. Contributions are
accepted under the repository licence. Until those files exist, the contributor rules are
`CLAUDE.md` and `AGENTS.md`, and every change must pass `check_task.sh` and, before
shipping, `/stack-audit`. Never commit `.env`.

## What is deliberately unfinished

- **Recorded TabPFN scores for third-party datasets stay local.** Only the Netflix
  scores (CC0 data) are committed, and a fitness test holds them to their dataset hash.
  KKBox, telecom and bank scores are excluded by `.gitignore` on purpose: they are scores
  of licensed rows. So CI and a fresh clone replay Netflix only, and any other dataset
  needs `scripts/record_scores.py`, which needs the TabPFN licence and `TABPFN_TOKEN`.
