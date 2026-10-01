# ADR-0008: Enforcing idempotency and approvals under Temporal, and as policy-as-code

- Status: **proposed: findings and recommendations, nothing decided**
- Date: 2026-09-24
- Layers: 7 (execution) and 8 (policy), exercised from 3 (runtime)
- Follows: ADR-0007 (Temporal spike findings)

## Question

ADR-0007 found that Temporal activities are at-least-once, so `gateway.execute` and its
idempotency ledger stay. Two things were left open:

1. **Under Temporal:** which of the gateway's guarantees survive being called from an
   activity, and which ones quietly stop holding?
2. **Policy as code:** could the authority rules (`decide`, `PreCommitPolicy` and the
   tiers in `require_approval`) live in Cedar or OPA/Rego without losing anything?

## What was verified, not assumed

Versions: `temporalio` 1.33.0 (dev server 1.32.0), `cedarpy` 4.12.1 and OPA 1.20.2. All
three were spike-only or are CLI tools; none is a runtime dependency. The
`regorus` package is not on PyPI, so Rego runs on the `opa` binary.

The E-probes drive the **real** gateway, ledger, approval store and approver directory
(`build_stack`) against a disposable Postgres database (`agentstack_spike_enforcement_test`).
The C-probes compare engines against the **real** `decide` and `require_approval`. Logs are
in `docs/evidence/temporal/RESULTS-enforcement.txt` and `docs/evidence/policy/RESULTS.txt`.

## Under Temporal

| Probe | Result |
|---|---|
| E1 key derivation | Each row counts how many times the effect landed. The three scenarios are: a retry after the commit landed, `temporal workflow reset` to the first task, and a second workflow for the same effect (a redelivered trigger).<br>• **content-derived** (`refund:{tenant}:{charge}:{amount}`): 1 / 1 / 1<br>• `workflow_id+activity_id`: 1 / 1 / **2**<br>• `run_id+activity_id`: 1 / **2** / **2**<br>• attempt-derived: **2** / **2** / **2** |
| E2 UnresolvedEffect | **With the default retry policy**, the gateway held: the surface was called once. But the activity spent every attempt hitting IN_FLIGHT and the run ended `failed`, with nothing reconciled. Temporal's default `maximum_attempts` is **0, meaning unlimited** (`temporalio/common.py`). **With the error marked non-retryable**, there was one attempt, then the run parked, a reconciler finalized the key, the re-run deduplicated, and the effect landed once. |
| E3 approval at the act | **Snapshot captured when the approver was asked and passed into the commit activity:** the world changed afterwards, and the refund **still committed**. **Snapshot read inside the activity:** `ApprovalStale`, no commit. **Unauthorised approver:** the Update validator **accepted** the answer, and `authorize_approver` in the grant activity refused it, so nothing committed. |
| E4 gateway-only | Without an interceptor, an activity that calls the surface client directly **committed**. With an allowlist `ActivityInboundInterceptor`, it was refused as `UndeclaredActivity`, non-retryable, with no commit. A workflow module that imports `agentstack.execution.gateway` **passes the sandbox and runs**. |

### Rules this produces

1. **An idempotency key is the business identity of the effect, never a Temporal
   identifier.** `workflow_id`, `run_id`, `activity_id` and `attempt` all fail at least one of
   retry, reset or redelivery (E1). The tool builders already derive keys from content. Nothing
   stops a future builder from reading `activity.info()`, though, so that needs a fitness test.
2. **Gateway refusals are non-retryable, and `UnresolvedEffect` parks the run.**
   `UnresolvedEffect`, `ApprovalStale`, `ApprovalRequired`, `PolicyDenied`,
   `SandboxViolation` and `ApproverNotAuthorized` go in `non_retryable_error_types`, via one
   shared `RetryPolicy` rather than one per call site. `UnresolvedEffect` routes to a
   reconcile wait (E2). Retrying a refusal does not change the answer; it only postpones
   admitting it.
3. **The commit activity reads the state snapshot itself, at the act.** It never takes one
   as an argument from workflow state. Carrying the approval-time snapshot through the
   workflow makes the staleness check compare the snapshot with itself, and a stale approval
   commits (E3, first row). This is the Temporal version of "approval immediately before the
   irreversible act".
4. **An Update validator checks shape and workflow state, never authority.** It runs in the
   deterministic sandbox and cannot consult the approver directory, and E3 shows it
   accepting an unauthorised approver. Authority stays in layer 8: `authorize_approver`
   runs in an activity and writes the `approvals` row, and the gateway requires that row.
5. **Only declared activities run, and workflow modules never import execution or storage.**
   A worker interceptor enforces the first (E4). The sandbox does *not* enforce the second
   (E4), so it needs an import-linter contract:

   ```ini
   [importlinter:contract:N]
   name = Workflow code has no path to an effect (Runtime, workflows, durable execution and Execution surfaces)
   type = forbidden
   source_modules = agentstack.runtime.workflows
   forbidden_modules =
       agentstack.execution
       agentstack.storage
   ```

   This contract is drafted, not tested: `agentstack.runtime.workflows` does not exist yet.
   The interceptor allowlist only sees activity *names*. What a declared activity does
   inside is still governed by the existing contract 3 (only the gateway touches a surface
   client).

### Open question (not probed)
An approval has no expiry. A commit activity that retries with backoff could act hours
after the "yes". Rule 3 catches a changed world, but not "the approver would no longer
say yes to this". Consider an approval TTL, checked in `require_approval`.

## Policy as code

| Probe | Result |
|---|---|
| C1 differential | All 480 combinations of: envelope live, acts-as, scope, tenant, tier, reversibility, approval on record (none/human/policy) and snapshot freshness. **Cedar 0 and Rego 0** disagreements with the Python verdict. A planted bug ("a policy grant satisfies ALWAYS") showed up in **2 of the 480** cases, and the test caught it. |
| C2 audit | Both engines return **every** refusal reason, and the reason sets are identical. 385 of 463 refusals have more than one reason, while the Python audit records only the first. Cedar gives policy ids (`policy0`…), so rule names need an `@id` annotation map. Neither engine has "first match", so the precedence order used for `policy_decision` stays outside the engine. |
| C3 untrusted input | **Cedar**: a policy that reads `context.model_output` fails schema validation, and a request carrying that field is refused at evaluation. **Rego**: `opa check` passes without an input schema and fails with one (`-s`). At runtime, OPA accepts the extra field and ignores it. |
| C4 cost (p50 / p99) | Python 3µs / 7µs. Cedar in-process 92µs / 104µs. OPA sidecar over HTTP 238µs / 491µs. All are small next to the gateway's own Postgres round trips. |

Other differences:
- **Cedar** cannot express "the resource starts with the envelope's tenant", because `like`
  only takes literal patterns. The caller has to parse the tenant out of the resource, so a
  small part of the authority logic moves into Python.
- **Rego** can express it, but it needs a sidecar process (or the `opa` binary) and has no
  schema by default.

### Verdict: **defer. Keep the rules in Python, and import two things the engines showed.**

The authority rules are six conditions that change together with the code, reviewed by the
same people. Neither engine decided anything differently (C1). What they add is **all
reasons in the audit** (C2) and **schema-level refusal of untrusted fields** (C3, Cedar
only). Both can be had without a new runtime dependency:

- Record the full refusal set in the audit alongside `policy_decision` (the first rule),
  collected the way `PreCommitPolicy.refusals` already does.
- Make the 480-case exhaustive differential a fitness test for the Python policy. A planted
  bug that only 2 of 480 inputs expose is the kind a hand-picked test misses.

**Revisit** if rules need to be changed by people who don't ship code, or per tenant. If that
happens, choose **Cedar**: it runs in-process, its schema enforces the untrusted-content
boundary structurally, and forbid-overrides-permit matches "checks are conjunctive". Budget
for the resource-tenant parse moving to the caller.

## Proposed `/spec` tasks, if accepted

Each task names its layer and the test that proves it.

| Task | Layer | Proving test |
|---|---|---|
| Idempotency keys never derive from Temporal identity | 6/7 | `test_idempotency.py`: builders take no `temporalio` import; retry/reset/redelivery land once |
| Gateway refusals are non-retryable; `UnresolvedEffect` parks for reconcile | 3/7 | `test_unresolved_effects.py`, re-pointed at the Temporal runtime |
| The commit activity reads the snapshot at the act | 3/8 | `test_state_snapshot.py`: world changes after approval → `ApprovalStale` |
| The Update validator is shape-only; authority is an activity | 8 | `test_approver_authorisation.py`: an unauthorised Update is accepted and the commit is refused |
| Activity allowlist interceptor + workflow import contract | 3/7 | `test_layer_boundaries.py` + the new `.importlinter` contract (**ask first**) |
| Audit records every refusal reason | 9 | `test_audit_separate_from_traces.py` |
| Exhaustive authority differential | 8 | new `tests/fitness/test_authority_exhaustive.py` |

None of this touches `src/`, `.importlinter` or `CONSTRAINTS.md` yet.
