# Constraints

Last reviewed: 2026-09-21 by @mzjd97

This is the bar. It outlives the session, it is read before any code is written, and
it does not get weakened to make a change pass. Every numbered row names the command
that produces the verdict — a number with no command in the `Checked by` column is an
aspiration, not a constraint.

## Floor (always enforced, no setup required)

Generic (from `constraint-driven-development`):

- No new suppression comments: `# type: ignore`, `# noqa`, `# nosec`, `# pragma: no cover`
- No unimplemented stubs: bare `raise NotImplementedError`, empty `except: pass`
- No skipped or deleted tests without a reason in the commit message
- No secrets in source
- This file, `STACK.md`, and `.importlinter` do not get weakened to make a change pass

Stack-specific (from `STACK.md`):

- No module outside `agentstack.execution` imports an HTTP client, shell, socket or mailer
- No module outside `agentstack.storage` imports a database driver — the agent's own
  state is substrate (layer 10), not an execution surface (layer 7); see docs/adr/0005
- No migration is edited after it has been applied, and no version gap is tolerated
- No tool registers without complete capability metadata
- No side-effecting tool without an idempotency policy and an approval tier
- No write to the memory store outside `agentstack.context.memory.write()`
- No span type removed from `observability.spans.REQUIRED_SPANS`
- No context item without scope, provenance, freshness, reason and trust
- No tool argument reaching a prepare function unvalidated against its input_schema
- No irreversible tool without a reversal note the approver is shown
- No read entered in the idempotency ledger, and no read committed as an effect
- No side effect committed without a claimed idempotency key, and no blind retry
  of a claim that was never settled
- No run advancing past an unsatisfied wait
- No graph node advancing a run whose checkpoint names a different run
- No `metric_movement` trigger reaching a propose — looking is not deciding, and a
  look that can also advance an experiment is an optional-stopping machine
- No trigger evaluated twice: brokers redeliver, and a second evaluation asks a
  human again about a decision they already made
- No checkpoint written asynchronously — durability that loses the last step is not
  durability
- No vendor-owned tables in `public`: a library that migrates its own schema gets a
  schema of its own, so one namespace never carries two migration ledgers
- No declared containment dimension that `Sandbox.check()` does not read
- No cohort column dropped without a recorded reason, and no `data_as_of` derived
  from a clock — a watermark that moves when nobody looked cannot identify a population
- No churn score taken in sample — every row is scored by a model that did not see
  its label, or the top decile is the rows the model fit best
- No scorer decides who gets an offer; it returns a probability and policy does the rest
- No README claim that nothing checks: the licence constraint and the counts it
  states are asserted by `tests/fitness/`
- No cohort widened to meet a gate — a cut that is adjusted until it qualifies is
  not a cut; refuse and say which gate stopped it
- No value at risk from a modelled quantity: the floor is observed ARPU, and a
  dataset without one is loadable and not targetable

## Enforced with numbers

| Dimension | Rule | Checked by | Runs at |
|---|---|---|---|
| Lint | Zero errors from our config | `uv run ruff check .` | every edit |
| Format | Formatted | `uv run ruff format --check .` | every edit |
| Types | Zero errors under strict mypy | `uv run mypy` | every edit |
| Layer boundaries | Zero contract violations | `uv run lint-imports` | every edit |
| Capability metadata | 100% of registry entries complete | `uv run pytest tests/fitness/test_tool_registry.py` | every edit |
| Secrets | No secrets in source | `gitleaks detect --redact --no-banner` | CI (skipped locally if not installed) |
| Architecture fitness | All fitness tests green | `uv run pytest tests/fitness` | task end, CI |
| Approval coverage | 100% of irreversible tools gated | `uv run pytest tests/fitness/test_approval_boundary.py` | task end, CI |
| Trace completeness | All required spans on a golden run | `uv run pytest tests/fitness/test_trace_completeness.py` | task end, CI |
| Coverage | Changed lines >= 80% covered | `uv run pytest --cov=agentstack --cov-report=lcov` + `git diff` | task end, CI |
| Schema migrations | Up from empty, down, and up again on a fresh database | `uv run pytest tests/infra` | task end, CI |
| Cohort identity | Same data in, same `data_as_of` out | `uv run pytest tests/fitness/test_data_snapshot.py` | every edit |
| Live cohort checks | Real datasets match `data/manifest.json`; no feature correlates with the target above 0.9 | `uv run pytest tests/live` | **locally, before a cohort is used in an experiment**. Not on PRs: CI has no Kaggle credentials. A scheduled run needs those secrets configured first — until then this row says only what is true. |
| Licence gate | A missing or invalid `TABPFN_TOKEN` is refused at startup | `uv run pytest tests/fitness/test_prediction_gate.py` | every edit |
| Live model checks | Recorded scores still match the real model; scores separate churners; scoring is reproducible | `uv run pytest tests/live` (needs `TABPFN_TOKEN` and `--extra prediction`) | **locally, before a cohort is used in an experiment** |
| Cohort reproducibility | Same snapshot + same model version → same cohort, same `experiment_version` | `uv run pytest tests/fitness/test_targeting.py` | every edit |
| Turn checkpointing | Sync durability; a died turn resumes **from Postgres** without re-calling the model | `uv run pytest tests/fitness/test_turn_graph.py` | every edit |
| Bar integrity | No weakened constraint in the diff | `uv run python scripts/stack_guard.py --base main` | task end, CI |
| Release gates | 100% of Part-8 gate evals pass | `uv run python -m evals run --gates` | CI |
| Dependencies | Nothing at high or above | `osv-scanner scan source -r .` | CI |

Why these numbers:

- **80% of changed lines**, not project coverage: high enough to force a test, low enough
  to allow a config line, and it is a number this change can actually move.
- **Zero** for layer, metadata, approval and trace rows: these are invariants, not
  gradients. One violation is a collapsed layer.
- **High and above** for dependencies: below that is mostly noise.

**Three lines in `prediction/engine.py` cannot be covered in CI.** They construct and
call `TabPFNClassifier`, and they do not execute anywhere the optional extra and the
licence are absent — which is CI, by design. They are exercised by `tests/live`. The
alternatives were a `pragma` (banned by the floor), installing torch in CI to raise a
percentage, or pretending.

This briefly took project coverage to 98% at T6; it is back at 99% because the
checkpoint-pinning work that followed added more tests than uncovered lines. The
ratchet says what is true today, not what was convenient at the time.

## Measured, not yet enforced

| Metric | Today | Direction |
|---|---|---|
| Project coverage | 99% | must not fall (tolerance 0.5%) |
| Fitness test count | 28 | must not fall |
| Required span types | 9 | must not fall |
| p95 turn latency | not yet measured | record before first deploy |
| Cost per turn | not yet measured | record before first deploy |

## Files exempt from the pattern scan

`scripts/stack_guard.py`, this file, and `tests/fitness/test_the_bar_guards_itself.py`
are not scanned for `# noqa`, `NotImplementedError` or `skipif`, because naming those
patterns is what all three are for. The exemption is a named tuple in the guard, and a
test asserts its exact contents so it grows only deliberately.

This is not a hole in the guard: `CONSTRAINTS.md` is still checked for removed floor
bullets, removed enforced rows and new exceptions, which is where a real weakening of
it would show up.

## The live lane

`tests/live` is excluded from the default suite in one visible place — `addopts` in
`pyproject.toml` — because it needs third-party datasets that CI has no credentials
for. It is a directory, not a `@pytest.mark.skipif`: a conditional skip would trip the
floor above, and the tempting fix for that would be to loosen the floor.

What that costs, stated rather than implied: CI proves the loader, the watermark, the
drop rules, the licence gate, the fold assignment and the replay guards are correct.
It does not prove the data upstream is unchanged, and it does not prove TabPFN still
returns what we recorded. That is what the live lane is for, and it has to actually
be run.

**Not yet recorded.** `data/scores/` is empty: producing it needs a `TABPFN_TOKEN`,
and none is configured on this machine. Until it exists, `tests/live` fails with the
command that fixes it rather than skipping — the same posture as a missing database.

## Where checks run (cost decides placement)

| Stage | Command | Budget |
|---|---|---|
| Every edit | `bash scripts/check_fast.sh` | < 5s, changed files only |
| Task end / turn end | `bash scripts/check_task.sh` | < 90s |
| CI | `bash scripts/check_full.sh` | minutes |

A check that stalls the loop gets switched off, and a gate people switched off is worse
than no gate. If a stage goes over budget, move the check outward — do not delete it.

## External opinions

At least one constraint must be judged by something other than tests we wrote
ourselves. Here: `osv-scanner` (vulnerability database), `gitleaks` (secret patterns),
`mypy --strict` and `import-linter` (rules we cannot argue with at runtime).

## Amendments to the floor

A floor rule can be replaced. It cannot be replaced quietly: `stack_guard` reports a
removed or reworded floor bullet until the old text appears here with a reason. An
exception says "not here, for now"; an amendment says "this rule was wrong".

| Date | Rule removed | Replaced by | Why | Recorded in |
|---|---|---|---|---|
| 2026-09-22 | No module outside `agentstack.execution` imports an HTTP client, DB driver, shell, socket or mailer | the same rule minus "DB driver", plus a driver rule naming `agentstack.storage` | The rule conflated two things layer 7 keeps apart. An execution surface is a system the agent acts *upon*, gated by policy, approval and containment. The agent's own state store is substrate: recording that a step completed is not an effect anyone approves, and `agentstack.context` cannot reach layer 7 at all under contract 2, so it could never be persisted. Paired with a tightening — `lint-imports` contract 5 now enforces the driver rule, which no layer contract did before (only an AST scan in `tests/fitness/test_layer_boundaries.py` did), and that scan now names which layer may hold which client instead of exempting `execution` from all of them. | [ADR-0005](docs/adr/0005-state-substrate-and-migrations.md) |

## Exceptions

| ID | Rule | Path | Reason | Owner | Expires |
|----|------|------|--------|-------|---------|
| — | — | — | none yet | — | — |

An exception needs an owner and an expiry. Deleting a constraint unblocks everyone
forever; an exception with a date unblocks one person for 90 days.
