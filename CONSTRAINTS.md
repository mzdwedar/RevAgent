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
- No rollout prepared without the cohort predicate it was approved against
- No approval asked as a bare percentage: the prompt names how many real customers
  are affected, and an ask cannot be built without that number
- No interaction accepted without signature, freshness **and** replay checks — a
  signature proves a payload is authentic, never that it arrived once
- No payload parsed before it is verified, and no unverified signature recorded
- No approver authorised against a tenant the interaction named: the tenant comes
  from the run the approval is bound to, or two correct checks compose into a hole
- No approval standing without a row somebody added, and a record of who added it
- No notification failure swallowed — a run parked on a question nobody received
  waits forever, and looks exactly like waiting patiently
- No tool exposed on a stage it does not belong to: drafting and rolling out are
  never on the same menu
- No `ALWAYS` action satisfied by a policy grant — a rule does not authorise an
  irreversible act by minting the record the tier demands
- No approval tier that behaves like another: `PRE_COMMIT` grants on a named rule
  and records it, `ALWAYS` wakes a person
- No write to the memory store outside `agentstack.context.memory.write()`
- No span type removed from `observability.spans.REQUIRED_SPANS`
- No context item without scope, provenance, freshness, reason and trust
- No tool argument reaching a prepare function unvalidated against its input_schema
- No irreversible tool without a reversal note the approver is shown
- No read entered in the idempotency ledger, and no read committed as an effect
- No side effect committed without a claimed idempotency key, and no blind retry
  of a claim that was never settled
- No run advancing past an unsatisfied wait
- No human-approval wait that does not record what it is asking about — the process
  that answers is not the one that asked
- No approval bound to a fingerprint recomputed at answer time: it binds the one the
  wait recorded, or it approves something other than what was shown
- No graph node advancing a run whose checkpoint names a different run
- No `metric_movement` trigger reaching a propose — looking is not deciding, and a
  look that can also advance an experiment is an optional-stopping machine
- No trigger evaluated twice: brokers redeliver, and a second evaluation asks a
  human again about a decision they already made
- No checkpoint written asynchronously — durability that loses the last step is not
  durability
- No durability claim proved by a same-process resume — the process that wrote the
  checkpoint still has the objects in memory, and a store that wrote nothing would pass
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
| Process death | A killed process's run resumes in a fresh one without re-calling the model | `uv run pytest tests/durability` | task end, CI |
| Approval tiers | Three tiers, three behaviours; a policy grant never satisfies `ALWAYS` | `uv run pytest tests/fitness/test_approval_tiers.py` | every edit |
| Model contract | The adapter reports what the model said; `num_ctx` and `think` are set explicitly | `uv run pytest tests/fitness/test_ollama_contract.py` | every edit |
| Live model checks | The real model still emits a well-formed tool call, deterministically | `uv run pytest tests/live/test_ollama.py` (needs Ollama + `qwen3:8b`) | **locally, before trusting a drafted candidate** |
| Candidate validation | A malformed, over-specified or unexposed proposal is refused before the registry is touched | `uv run pytest tests/fitness/test_experiment_tools.py` | every edit |
| Approval legibility | The prompt names a headcount, not a percentage | `uv run pytest tests/fitness/test_slack_outbound.py` | every edit |
| Callback authenticity | Bad signature, stale timestamp and replay each refused before any policy | `uv run pytest tests/fitness/test_slack_inbound.py` | every edit |
| Approver authorisation | A signed interaction from an outsider is refused in layer 8, not the adapter | `uv run pytest tests/fitness/test_approver_authorisation.py` | every edit |
| Approve to rollout | A human's answer grants a bound approval, satisfies the wait, and the rollout commits once — across a process death | `uv run pytest tests/fitness/test_approve_resume_rollout.py tests/durability` | task end, CI |
| Concurrency | 100 runs at once each commit exactly once with every resume delivered twice; a raced step completes once; a trigger batch evaluates each experiment once, within its bound; a migrator never unlocks over uncommitted work | `uv run pytest tests/durability/test_concurrency.py tests/infra/test_migrations.py` | task end, CI |
| Registry narrowness | Every registry tool: no `status`/`fields`/`patch`/`updates` setter, no array or object argument, only the rollout names a `percentage`, every write requires `experiment_version`, one scope per blast radius; the stage menus are exactly draft 5 / evaluation 5 / rollout 4; a halt that names anything but zero is denied by `decide`, whatever was approved | `uv run pytest tests/fitness/test_registry_tools.py tests/fitness/test_approval_tiers.py` | every edit |
| Request binding | Every id that reaches a resource path is one segment, and a declared `format` is enforced or refused; each tool prepares the verb its spec declares; the gateway denies and audits, before policy and approval and with the surface untouched, a request whose tool, surface, verb or fixed payload value (`halt_rollout`'s zero) is not its spec's; each registry verb's payload is refused by both clients unless it is exactly that verb's shape, and the store refuses a rollout not a whole 0–100 and a halt not 0 | `uv run pytest tests/fitness/test_request_binding.py tests/infra/test_registry_store.py` + `uv run python -m evals run --gates` | every edit |
| Registry preconditions | Each transition is refused from every wrong state and leaves every read unchanged; the fake and Postgres clients pass one contract suite; a lost answer then a retry leaves one row; twenty simultaneous halts leave one `halted` and one halt event, through the gateway and at the store alone | `uv run pytest tests/infra/test_registry_store.py tests/fitness/test_registry_tools.py tests/durability/test_concurrency.py` | task end, CI |
| Checkpoint compatibility | An incompatible checkpoint change ships a version bump, drops the old version, and a migration note — or fails the build | `uv run python scripts/checkpoint_guard.py --base main` | task end, CI |
| Bar integrity | No weakened constraint in the diff | `uv run python scripts/stack_guard.py --base main` | task end, CI |
| Release gates | 100% of Part-8 gate evals pass | `uv run python -m evals run --gates` | CI |
| Dependencies | Nothing at high or above | `osv-scanner scan source -r .` | CI |

Why these numbers:

- **80% of changed lines**, not project coverage: high enough to force a test, low enough
  to allow a config line, and it is a number this change can actually move.
- **Zero** for layer, metadata, approval and trace rows: these are invariants, not
  gradients. One violation is a collapsed layer.
- **High and above** for dependencies: below that is mostly noise.

**A few lines reaching a real serving system cannot be covered in CI.** Three in `prediction/engine.py` construct and
call `TabPFNClassifier`; two in `model/ollama_engine.py` construct the Ollama client.
None executes where the extra, the licence or a running model is absent — which is CI,
by design. All five are exercised by `tests/live`, which is why that lane exists.

The alternatives were a `pragma` (banned by the floor), installing torch and running a
model in CI to raise a percentage, or pretending. The ratchet says what is true today,
not what was convenient at the time: 99% at T11, 98% at T12 when the Ollama adapter
landed.

## Measured, not yet enforced

| Metric | Today | Direction |
|---|---|---|
| Project coverage | 98% | must not fall (tolerance 0.5%) |
| Fitness test count | 36 | must not fall |
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
