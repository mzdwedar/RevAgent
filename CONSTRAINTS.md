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

- No module outside `agentstack.execution` imports an HTTP client, DB driver, shell, socket or mailer
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
- No declared containment dimension that `Sandbox.check()` does not read

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
| Bar integrity | No weakened constraint in the diff | `uv run python scripts/stack_guard.py --base main` | task end, CI |
| Release gates | 100% of Part-8 gate evals pass | `uv run python -m evals run --gates` | CI |
| Dependencies | Nothing at high or above | `osv-scanner scan source -r .` | CI |

Why these numbers:

- **80% of changed lines**, not project coverage: high enough to force a test, low enough
  to allow a config line, and it is a number this change can actually move.
- **Zero** for layer, metadata, approval and trace rows: these are invariants, not
  gradients. One violation is a collapsed layer.
- **High and above** for dependencies: below that is mostly noise.

## Measured, not yet enforced

| Metric | Today | Direction |
|---|---|---|
| Project coverage | 98% | must not fall (tolerance 0.5%) |
| Fitness test count | 19 | must not fall |
| Required span types | 9 | must not fall |
| p95 turn latency | not yet measured | record before first deploy |
| Cost per turn | not yet measured | record before first deploy |

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

## Exceptions

| ID | Rule | Path | Reason | Owner | Expires |
|----|------|------|--------|-------|---------|
| — | — | — | none yet | — | — |

An exception needs an owner and an expiry. Deleting a constraint unblocks everyone
forever; an exception with a date unblocks one person for 90 days.
