# ADR-0010: Remove the billing catalog (`issue_refund`, `lookup_subscription`, `amount_cents`)

- Status: **accepted**
- Date: 2026-09-29
- Layers: 6 (tools), 8 (policy prompt), 1 (walkthrough), 9 (evals)

## Context

`agentstack.tools.catalog` began as a scaffold: two placeholder billing tools, a read
(`lookup_subscription`, `billing:read`) and an irreversible write (`issue_refund`,
`billing:refund`, `Approval.ALWAYS`, `amount_cents` in a `cents` format), so the layer
invariants had something concrete to be asserted against. SPEC.md said to replace them
with the experiment registry's tools. The registry tools landed beside them instead,
because the placeholder pair was the fixture for the walkthrough and about twenty
fitness tests, and deleting it would have destroyed working evidence.

This project chooses the best offering for a cohort from revenue. It does not track
subscription lifecycle and never issued a refund, so the placeholder was a second product
riding along in the tool registry, on the default stage.

## Decision

Remove the billing catalog and the money vocabulary that existed only for it, and carry
every invariant it proved onto the experiment tools:

| Was | Is |
|---|---|
| `issue_refund` (`ALWAYS`, irreversible, keyed) | `roll_out_variant_to_percentage`, which is all of those |
| `lookup_subscription` (read) | `get_experiment` / `list_experiments` (reads) |
| `amount_cents` + `format: cents` + money rendering in the approval prompt | removed; `percentage` is an integer bounded 0-100 |
| `billing:read`, `billing:refund` | `experiments:read`, `experiments:rollout` |
| `RecordingClient` fault injection (`fail_after_effect`, `refuse_before_effect`) | `FlakyRegistry` (tests) and `fake_registry` (evals), over the in-memory `RegistryClient` |

Consequences to know about:

- **The `default` stage now exposes no tools.** Tests and evals that drive a turn create
  the run on an explicit stage (`ROLLOUT_STAGE` for the irreversible path).
- **Nothing was deleted to pass.** Refund-based tests, eval cases and gate cases were
  ported, keeping their assertions; the eleven eval cases were renamed (`refund-*` to
  `rollout-*`) and are still gates. Where a refund-only assertion had no analogue (the
  money rendering) it was replaced by the closest legibility check (the prompt shows the
  target and the state it moves from), and the containment tests now scope a sandbox to
  one experiment, because the tenant check fires first and the sandbox is what is under test.
- **Idempotency keys change prefix** (`refund:` to `rollout:`). No migration: refund
  appears only in SQL comments of migrations 0003, 0006 and 0008, which are applied and
  not edited. No schema, checkpoint or recorded workflow history contained the tools.

## Deliberately left

- **`Surface.API`, `RecordingClient`, the `{tenant}/customers/` sandbox prefix** now have
  no tool that uses them. They are layer 7 infrastructure; narrowing the declared surface
  is its own decision and needs its own ADR.
- **`spikes/`** are point-in-time evidence with committed `RESULTS` files. Nothing imports
  them, and `spikes/temporal/_gateway.py` still calls `issue_refund` through the registry,
  so those spikes will not run against this tree. They are records of what was measured,
  not tests, and are not rewritten.
- **Legacy `surface_calls` in the eval runner** still counts the API client. The cases
  that use it assert zero and are about injection; new expectations use `rollouts`.

## Retained on purpose

`Surface.API`, `RecordingClient` and the `{tenant}/customers/` sandbox prefix have no
production tool using them. They stay as layer-7 containment and test-fixture
infrastructure (`tests/fitness/test_containment.py`, `test_read_path.py`,
`test_effects_keep_their_snapshot.py`, and others). Removing them is a separate decision
and needs its own ADR.

## Follow-ups, closed

`evals run --gates` stopped at its first frozen-cohort case: the runner called
`rollout_admission(...)` without `prior_rollout_event`. The runner now reads it from the
registry history as the worker does. Two stale gate cases then surfaced and were fixed
without touching any expectation: `a-draft-whose-answer-was-lost...` still named the old
`experiments:write` scope (now `experiments:draft`), and two frozen-cohort messages
omitted `prior_rollout_event`. All 20 gate cases pass.
