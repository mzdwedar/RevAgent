# Spec: Experiment Operator

Status: **approved** (revision 10)
Date: 2026-09-21

---

## Assumptions

These are inferences, not decisions you made. Correct any of them and I will revise before planning.

1. **The registry is ours.** Experiment drafts live in a store this system owns and versions, not in a third party's.
2. **The rollout is not ours.** Promoting a variant to 10% changes what real customers see, through an external API under a credential we hold. It is the only externally visible act in the system.
3. **TabPFN comes from the sibling benchmark** (`~/Desktop/revenuecat/src/churn_tabpfn`) as a library, not a service. The benchmark's protocol — 5-fold stratified CV, seed-pinned, ROC-AUC/PR-AUC headline, calibration via Brier — carries over as the prediction contract.
4. **Dev data is the Kaggle datasets** already registered in that benchmark. Production subscriber data comes from somewhere else and is an open question.
5. **One capability, not several** (Phase 0 scope check). The trigger, the prediction, the policy, the draft and the rollout are steps of one workflow with one consumer — the run itself. No capability map; if the registry later grows its own consumers, that is when it earns a module id.
6. **Approvers are a named Slack group**, not whoever happens to see the message.
7. **One primary metric, the rest as guardrails** — *decided*: Incremental Net Saved
   Value is primary; conversion and churn are guardrails. TabPFN does **both**
   targeting and adjustment.
8. **Randomisation happens inside the targeted cohort.** Assumed because the
   alternative is not a design choice, it is a broken experiment — see below.

---

## Objective

Each RevenueCat experiment becomes a **durable run that lives for weeks**.

The run is **trigger-based, not scheduled**, and it responds to two kinds of trigger:

- **`data_arrival`** — an upstream batch landed and the watermark advanced
- **`metric_movement`** — a monitored statistic crossed a watch threshold

### The experiment lifecycle

An experiment has two halves, and they ask different questions:

```
   +-- iteration 1 ---------------------+  +-- iteration 2 --------------+
   score -> target -> propose -> launch     measure -> promote or stop
   "who is at risk, and is intervening      "did intervening actually
    on them worth trying?"                   save more than it cost?"
```

The first half needs a churn model and no statistics — nothing has happened yet to
measure. The second half needs statistics and no new model. That is why they split
where they do, and the split is the product's, not a convenience.

Either trigger wakes the run. It pulls the new cohort data, scores it with TabPFN, evaluates a deterministic decision policy, and does exactly one of three things:

- **continue** — not enough evidence yet; go back to waiting
- **abstain** — the evidence says stop, or a guardrail tripped; explain why and stop
- **propose** — draft the rollout, store it in the registry, and ask a human in Slack. On approval, roll the winning variant out to ~10% **of the targeted cohort**.

The run survives process restarts, deploys, and long stretches of nothing happening.

**Why this and why now.** The rails built so far assert Part 4's invariants against waits that last microseconds in a test. A run that waits days for a trigger and resumes correctly after a deploy is the real version of that layer — durable execution chosen "based on retry, wait and resume needs, not demo convenience". Trigger-based waiting strengthens the case rather than weakening it: a timer at least tells you when it will fire, whereas a trigger that may never fire is the harder problem, and the one Part 4 names explicitly as needing a wake-up mechanism that handles indefinite waits.

**The language model's job is small, deliberately.** TabPFN predicts. The policy is code. The language model drafts the proposal a human reads and explains an abstention in terms a human can check.

**And the prediction model's job is bounded too, in both halves.** In iteration 1
TabPFN does **targeting**: it scores churn risk on pre-treatment features and selects
who is eligible. In iteration 2 it additionally does **variance reduction**, as a
covariate that makes a real effect detectable with less data.

Neither use lets it decide anything. Targeting chooses who is studied — randomisation
inside the cohort keeps the estimate unbiased however good or bad the model is. The
covariate is pre-treatment, so it cannot bias the effect either; it can only fail to
sharpen it. A predicted quantity never stands in for behaviour that has not happened,
which is the difference between a model that saves time and a model that is quietly
running the company.

---

## Looking is not deciding

The two trigger kinds do not carry the same authority, and this is the load-bearing
design decision in the spec.

Waking on `metric_movement` means the run evaluates **precisely when noise is largest**.
If that wake could also propose a rollout, the system would be an optional-stopping
machine: biased toward acting on the looks that flatter the variant. Waking on
`data_arrival` has no such bias — a watermark advancing is independent of what the
data says.

So **looking** and **deciding** are separated:

| | `data_arrival` | `metric_movement` |
|---|---|---|
| Refresh evidence, record a cycle | yes | yes |
| Trip a guardrail → **abstain** | yes | **yes** |
| Reach an analysis point → **propose** | yes | **never** |

A `metric_movement` trigger can only ever stop an experiment, never advance one. The
asymmetry is deliberate and it is not a compromise: stopping early for harm needs no
protection against false positives in the way stopping early for benefit does. You do
not owe statistical rigour to the claim "this is hurting people, stop".

The trigger controls **when we look**. The inference rule controls **when we may
decide**. Keeping those apart is what makes both trigger kinds safe to have.

---

## Scope: iteration 1

**This iteration builds orchestration and runtime, and everything required to exercise
them honestly.** The statistics, the real predictions and the observability hardening
are later iterations. That is a scope decision, not an admission — a durable runtime is
a complete thing, and it is the part everything else hangs off.

### In scope

| Area | What gets built |
|---|---|
| Runtime | the LangGraph graph, Postgres checkpointer, run identity, recorded step boundaries |
| Waits | trigger waits and approval waits, with deadlines and stalled detection |
| Resume | resume across process death and across deploys, including **checkpoint schema migration for live runs** |
| Concurrency | many experiments running at once: trigger fan-out without stampede, checkpointer under contention |
| Side effects | the registry write and the rollout call, behind the existing gateway — claim/finalize, approval binding, policy denial |
| Ingress | a trigger endpoint carrying a `data_as_of` watermark |
| Candidate drafting | the model proposes the candidate; validation, policy and the registry write behind it |
| Approvals | the wait/resume loop, the rendered prompt, and **Slack interactive approval** end to end |

### Prediction is real from day one

TabPFN does the churn scoring in iteration 1. It is zero-training — a forward pass over
pre-treatment features — so there is nothing to stub around, and stubbing it would
throw away the part of this system that is actually about churn.

| Component | Iteration 1 | Iteration 2 |
|---|---|---|
| TabPFN **classifier** -> `y_churn` | **real** — drives targeting | unchanged |
| Risk threshold -> eligible cohort | **real** | unchanged |
| TabPFN **regressor** -> `y_spend` | — | added, for the covariate |
| `eLTV_pre` composite | — | added |
| `decide()` | **real** — a targeting and value decision | replaced by the effect decision |
| ANCOVA, Incremental Net Saved Value | — | added |

The split is clean because it follows the lifecycle. Iteration 1's `decide()` answers
*"is this cohort worth intervening on?"* from predicted risk and value at risk — a real
decision, deterministic, over real model output. Iteration 2's answers *"did the
intervention pay?"*, which cannot be asked before a rollout has run.

It also lands exactly on the ground the sibling benchmark already covered: the
**classifier** is the arm it validated, on real churn datasets, with a seed-pinned CV
protocol and calibration curves. The regressor is new ground and arrives in iteration 2
where it belongs.

### Data

Dev data is the Kaggle datasets already registered in `churn_tabpfn` — `telecom-bigml`
and `bank-churn`, both cleaned, both with their leakage traps already documented there.
`data_as_of` is the dataset snapshot.

`TABPFN_TOKEN` becomes a real dependency of this service in iteration 1, not a later
concern. The licence gate is a startup check, not a runtime surprise.

### The end-to-end path

One walkable path is the acceptance test for this iteration. Every arrow is real: a
real checkpoint, a real side effect, a real human in Slack.

```
trigger (data_as_of)
   └─▶ evaluate                      real TabPFN scoring, real targeting
        └─▶ propose a candidate      the variant worth testing on the cohort
             └─▶ create experiment   registry write — PREPARATION, reversible, ours
                  └─▶ park a wait    run sleeps; process may die here and must not care
                       └─▶ Slack     the rendered prompt, with headcount and dollars
                            └─▶ human approves
                                 └─▶ resume       the wait is satisfied, not bypassed
                                      └─▶ roll out   COMMITMENT, external, approved
```

The split is the one the gateway already enforces: creating the experiment is
preparation and needs no approval; rolling it out is commitment and cannot happen
without one bound to this run, this action and this state.

### Slack is an untrusted ingress

Putting the approval in Slack means the authenticity of every approval now rests on the
channel, and this is the one place the iteration could quietly undo everything the
approval machinery is for. A forged interaction payload is a forged approval.

Three separate checks, and they belong in three different layers:

1. **The request really came from Slack** — signature over the raw body plus a
   timestamp freshness check, rejecting replays. This is the adapter's job, in layer 1.
2. **The claimed user is who Slack says** — the adapter passes the user id onward as a
   *claim*. It does not decide anything with it.
3. **That user may approve this** — membership of the approver group, checked in
   layer 8, where every other authority question is answered.

The spec's standing rule holds: interfaces carry events, they never resolve identity or
policy. The adapter authenticates the *transport*; policy authorises the *person*.

### Deferred, and why it is safe to defer

| Deferred | Why it does not block the runtime |
|---|---|
| The **effect** statistics — ANCOVA, Incremental Net Saved Value | nothing has been rolled out long enough to measure; the question cannot be asked yet |
| The regressor and the `eLTV_pre` covariate | both exist to sharpen an effect estimate, and there is no effect estimate in iteration 1 |
| **Observability hardening** — PII redaction, retention, sink separation policy | tracing and audit already exist and the fitness tests depend on them; what is deferred is the *hardening*, not the instrumentation. Nothing is removed. |
| **The credential store** | `credential_ref` stays a string and the three real secrets — Slack bot token, Slack signing secret, the rollout credential — live in the environment. The envelope's shape is already right; what it points at is iteration 2. |

Two of these are debts with a name, recorded so they surface rather than rot: PII in
the trace store is a real exposure the moment production data lands, and environment
variables are not a secret store. Neither blocks iteration 1; both block anything
touching a real customer.

Note what is *not* on that list. The Slack signing secret is deferred as a **storage**
question only — verifying the signature is in scope, because an unverified approval
channel would hollow out the entire approval design rather than merely be untidy.

---

## Targeting and the primary metric

> **Iteration 2.** Recorded here because the design is decided and the interfaces above
> are shaped to receive it. Nothing in this section is built in iteration 1.

The question this system answers is no longer "which variant is best overall". It is:
**did intervening on this targeted high-risk cohort save enough money to justify the
offers we gave away?**

### Targeting

TabPFN runs on pre-treatment features to score churn risk. The high-risk cohort becomes
the **eligible population** for the experiment. Randomisation happens *within* that
population.

What targeting does and does not affect is the thing to be precise about:

- **Internal validity is untouched.** Randomisation inside the cohort means τ is an
  unbiased estimate of the effect *on that cohort*, no matter how good the targeting
  model is. A bad model cannot produce a wrong answer.
- **External validity is narrowed, deliberately.** A bad model means a valid experiment
  on the wrong people — the true effect, measured on a cohort you did not intend to
  study. That is a real risk and it is not detectable from inside the experiment.

So the targeting model is frozen at experiment start and carried in
`experiment_version`. If the model changes, the eligible population changes, and it is
a different experiment wearing the same id.

### The primary metric

```
Incremental Net Saved Value
  = (Saved LTV_treatment − Saved LTV_control) − Total Cost of Offers Deployed
```

Three properties worth naming:

- **It is denominated in dollars**, so the decision threshold is a business quantity
  rather than an arbitrary effect size. "Is +$12k net worth a 10% rollout" is a
  question a human can actually answer.
- **It internalises the cost of intervening.** A variant that retains customers
  expensively correctly loses. That cost term is observed exactly — we know what we gave
  away — and it is incurred only in the treatment arm, so the metric is deliberately
  asymmetric rather than a plain difference of like for like.
- **The counterfactual lives in the control arm, not in a model.** That is what makes
  the word "saved" legitimate here, and it is conditional on the constraint below.

### Regression to the mean is the trap

Selecting the highest-risk users selects, among others, users whose risk score is
transiently inflated by noise. Those users would have looked better next month whether
or not anyone intervened.

A control arm randomised **within the targeted cohort** absorbs this completely: both
arms regress equally, and the difference is clean. A control arm drawn from the general
population does not, and the experiment will manufacture a large, confident, entirely
fictitious saving.

This is the single easiest way for this design to produce a wrong answer that looks
right, so it is a **Never** and a success criterion rather than a paragraph.

### TabPFN does both jobs

Targeting chooses *who enters*; adjustment sharpens *the estimate on whoever entered*.
Different stages, no interference:

```
X_pre ──▶ TabPFN Classifier ──▶ ŷ_churn ──▶ risk score ──▶ eligible cohort
       │                                                         │
       │                                                    randomise
       │                                                    ╱        ╲
       │                                              treatment    control
       │                                                         │
       └─▶ Classifier ŷ_conv, Regressor ŷ_spend ──▶ eLTV_pre ──▶ ANCOVA covariate
                                                                   │
                                            outcome ~ treatment + eLTV_pre ──▶ τ
```

Both TabPFN calls run on pre-treatment features, before randomisation. The adjustment's
safety property holds unchanged: **a pre-treatment covariate cannot bias τ.** A
miscalibrated or drifted model can only fail to tighten the estimate, never move it.

Incremental Net Saved Value is noisier than a plain revenue outcome — a difference of
heavy-tailed sums, minus a cost — so the adjustment is doing more work here than it
would have on the previous metric.

### One catch: targeting weakens its own covariate

The covariate loses power inside a targeted cohort, and it is worth understanding why
rather than discovering it in a wide confidence interval.

Variance reduction works by explaining spread in the outcome. Targeting deliberately
removes spread: everyone enrolled is high-risk, so `ŷ_churn` has almost no variance
left within the cohort. A covariate with no variance explains nothing. **Range
restriction attenuates exactly the correlation the adjustment depends on.**

This is the argument for using the composite rather than the raw risk score. Within a
high-risk cohort, `ŷ_churn` is nearly constant — but `ŷ_spend` is not, because
high-risk customers still differ enormously in what they are worth. So `eLTV_pre`
retains useful variance precisely where its churn component has none, and the
composite earns its keep in a way the risk score alone would not.

Practical consequence: measure the covariate's realised correlation with the outcome
per experiment and record it. A covariate that stops correlating has stopped working,
and it will not say so.

---

## Tech Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python ≥3.11, uv | unchanged |
| Durable execution | **Temporal** (runs), **LangGraph** (the turn, inside one activity) | Superseded 2026-09-24 by `SPEC-durable-runtime.md` (ADR-0007). Was: LangGraph alone. The Postgres checkpointer stays for the turn graph. |
| Checkpoint store | Postgres | must survive a deploy, so not in-process |
| Prediction | TabPFN-3.5 classifier (iteration 1), regressor (iteration 2) | licence-gated weights; the classifier is the arm the sibling benchmark validated |
| Model serving (dev) | Ollama on Apple silicon | `num_ctx` is a value we set, not the model's nominal window |
| Model serving (prod) | cloud GPU, stack open | vLLM / TGI / SGLang / managed — open question |
| Channels | Slack (approvals) + **event ingress** (triggers) | two entry points, one interface layer |
| Registry | Postgres | ours, versioned |

---

## Commands

```
uv sync
uv run agentstack                                   # the walkthrough (existing)
bash scripts/check_fast.sh                          # every edit, <5s
bash scripts/check_task.sh                          # turn end, <90s
bash scripts/check_full.sh                          # CI
uv run pytest tests/fitness -v                      # the architecture bar
uv run python -m evals run --gates                  # release gates
uv run python scripts/stack_guard.py --base main    # did the bar move?
```

New, to be added by this spec:

```
uv run operator fire --experiment <id> --trigger <kind>   # inject a trigger, for development
uv run operator status --experiment <id>                  # where is this run, what is it waiting on
uv run operator resume --wait <id>                        # satisfy a wait by hand
uv run operator stalled --older-than 7d                   # runs whose trigger never came
```

`stalled` exists because trigger-based waiting can fail silently. A run that is waiting is indistinguishable from a run that is stuck unless something asks.

---

## Project Structure

The nine layers stand. This adds to them rather than beside them.

```
src/agentstack/
  interfaces/
    slack.py            NEW  approvals in, interactions out
    triggers.py         NEW  the event ingress; a trigger is an inbound event
  control_plane/
    experiment.py       NEW  one session per experiment; its decision transcript
  runtime/
    graph.py            NEW  the LangGraph workflow; checkpointer wiring
    evaluation.py       NEW  one evaluation cycle as a sequence of recorded steps
  context/
    evidence.py         NEW  assembles the cycle's working set: cohort stats,
                             predictions, policy verdict, prior decisions
  tools/
    catalog.py          REPLACE the placeholder refund tools
  execution/
    surfaces.py         registry + rollout clients land here (and nowhere else)
  prediction/           NEW  TabPFN adapters (classifier + regressor), the frozen
                             targeting predicate, and the frozen eLTV_pre
                             combination — a layer-4-shaped seam for a
                             non-language model
experiments/            NEW  policy definitions, thresholds, guardrails
```

`prediction/` is deliberately its own thing. TabPFN is an inference engine that is not the language model, and collapsing it into `model/` would make "the model engine" mean two things — the exact ambiguity Part 1 is against.

---

## Code Style

Unchanged, and the existing code is the reference. A tool prepares, it never acts:

```python
ROLL_OUT = ToolSpec(
    name="roll_out_variant_to_percentage",
    description=(
        "Promote one experiment variant to a percentage of the targeted high-risk "
        "cohort. Never to customers outside the cohort the experiment measured."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": {"type": "string", "format": "id"},
            "experiment_id": {"type": "string", "format": "id"},
            "variant_id": {"type": "string", "format": "id"},
            "percentage": {"type": "integer", "format": "percent"},
            # The cohort is part of the action, not context around it: a rollout is
            # only reproducible if the population it targets is.
            "targeting_model_version": {"type": "string", "format": "id"},
            "risk_threshold": {"type": "number"},
            "estimated_customers": {"type": "integer"},
        },
        "required": [
            "tenant",
            "experiment_id",
            "variant_id",
            "percentage",
            "targeting_model_version",
            "risk_threshold",
            "estimated_customers",
        ],
    },
    acts_as=ActsAs.DELEGATED,
    scope="experiments:rollout",
    surface=Surface.API,
    side_effecting=True,
    reversible=False,
    approval=Approval.ALWAYS,
    idempotency=Idempotency.KEY,
    reversal_note=(
        "rolling back to 0% stops new exposure but does not un-show the variant "
        "to customers who already saw it, or undo what they did about it"
    ),
    stages=frozenset({"rollout"}),
)
```

Note `stages={"rollout"}`: the rollout tool is not exposed on a cycle that is merely evaluating. Blast radius is a per-run decision, not a global one.

---

## Testing Strategy

pytest, `tests/fitness/` for architecture, `evals/cases/` for release gates, 80% of changed lines, ≥98% project coverage held by ratchet. The existing 22 fitness tests and 11 gates continue to apply unchanged.

This feature adds a test level the project does not yet have: **elapsed time and absent events**. A run that waits a week for a trigger cannot be tested by waiting a week.

| Level | Covers | Mechanism |
|---|---|---|
| Fitness | the invariants in `STACK.md` | as today |
| **Durability** | restart, deploy, resume, replay, stalled waits | injectable clock and injectable event source; kill and rebuild the process from the checkpoint store between cycles |
| Gates | the Part-8 release criteria | `evals/cases/` |
| **Policy** | the decision rule itself, and the trigger asymmetry | replay historical experiments with known outcomes; the policy is pure, so this is cheap |

Two things the durability level must actually do, or it proves nothing. Resume has to rebuild from Postgres **in a fresh process** — a checkpoint read back by the process that wrote it demonstrates almost nothing. And the absent-trigger case has to be exercised deliberately: a run whose trigger never arrives is the failure mode this architecture invents.

The policy level is new and worth its own line: because `decide()` is a pure function, it can be run against historical experiments where the right answer is already known. That is the only test in the system that checks whether a decision was *correct* rather than well-formed.

---

## Layer Ownership Ledger

| # | Layer | Touched | What changes | Test that proves it |
|---|---|---|---|---|
| 1 | Interfaces | **yes** | two entry points: an event ingress for triggers, and Slack. Neither resolves identity or policy. | `test_layer_boundaries` (contract 4) |
| 2 | Control plane | **yes** | one session per experiment; the decision transcript is the experiment's history. `session_id` is the experiment run, never a user. | `test_session_ownership` |
| 3 | Runtime | **yes, heavily** | LangGraph graph; each evaluation a recorded step; two kinds of wait — `trigger` (may never fire, needs a deadline) and `human_approval`. The trigger kind is carried into the cycle and gates which decisions are reachable. | `test_waiting_is_state`, `test_wait_gates_the_run`, new durability tests |
| 4 | Model engine | **yes, narrow** | drafting and explanation only. Ollama in dev, cloud GPU in prod. | contract tests on `ModelEngine` |
| 5 | Context / memory | **yes** | the cycle's evidence bundle: cohort stats, predictions, policy verdict, prior decisions on this experiment. Prior decisions are durable memory, scoped to the experiment. | `test_context_assembly`, `test_memory_is_explicit` |
| 6 | Tools | **yes, replaced** | registry read/write, rollout. Stage-filtered so rollout is unexposed on evaluation cycles. | `test_tool_registry` |
| 7 | Execution surfaces | **yes** | two surfaces: our registry (DB) and the rollout API. Both behind the gateway. | `test_capability_is_not_execution`, `test_read_path` |
| 8 | Identity / policy / approvals | **yes** | the evaluation runs as a service account with **no rollout scope**. The approval is what mints an envelope that has one. | new: `test_approval_widens_authority` |
| 9 | Observability | **yes** | spans per cycle; audit per rollout; a trace spanning weeks that must still reconstruct. | `test_trace_completeness`, `test_audit_separate_from_traces` |
| 10 | Infrastructure | **documented** | Postgres (checkpoints + registry), the event source, a GPU inference endpoint. | Foundation Assumptions below |

**Layer 8 is the interesting one.** When a trigger fires, no human is present, so the run acts as a service account — and that account must not hold `experiments:rollout`. Approval is not a gate the run passes through holding the authority it always had; it is the event that produces a narrower, time-boxed envelope which *can* roll out. That inverts the usual shape, where approval is a checkbox in front of a credential the agent held all along.

---

## Foundation Assumptions

| Concern | Dev | Production |
|---|---|---|
| Delivery semantics | at-least-once from the event source | at-least-once — every evaluation must be idempotent |
| Consistency | Postgres, read-committed | same |
| Isolation / failure | single process, killed freely | rolling deploys mid-wait are expected, not exceptional |
| Trigger source | injected by hand | open question — see below |
| Model asset | `qwen3:8b` (5.2 GB, Apache-2.0) | same asset, on a GPU |
| Serving system | Ollama on Apple silicon | cloud GPU; stack open |
| Interaction contract | Ollama's native tool-call API; `think=False`, `temperature=0` | must be verified equivalent, not assumed |
| Context budget | `num_ctx = 8192`, set explicitly in `model/ollama_engine.py` | measured against the production row; the dev number is a floor, not a target |
| Latency budget | p95 per evaluation ≤ 60s | ≤ 60s; an event-driven run has no latency pressure, which is a luxury worth spending on evidence |
| Cost budget | — | per evaluation, recorded before first deploy |

Two things this table is designed to stop. **Compatible is not equivalent**: the same weights behind Ollama and behind a cloud serving stack are the same asset and a different system, and tool-call reliability is the property most likely to differ. **The context window is a working set**: `num_ctx` is a number we choose, and the evidence bundle must fit the production setting, not the laptop's. Ollama's own default is smaller than most people assume, and a context window discovered by watching answers get worse is exactly the Part 2 confusion this table exists to prevent — a serving-system setting mistaken for a property of the model. `think=False` belongs here for the same reason: qwen3 is a reasoning model, and its thinking is a property of how it is served, not an answer a human should be shown.

---

## Boundary Decisions

| Question | Decision |
|---|---|
| What owns session identity, and how is it distinct from authorization? | The experiment run owns the session. Authorization is per-cycle and per-action; a long-lived session accrues no authority over time. |
| What is the transcript store, and how is prompt context derived from it? | Postgres holds every cycle's decision and evidence. `context/evidence.py` assembles a bounded view per cycle; the transcript is never the prompt. |
| What is memory here, who scopes it, and when is it written? | Prior decisions on **this experiment** — scoped to the experiment, written explicitly at the end of a cycle, TTL = the experiment's life plus a retention window. |
| Which capabilities are exposed, and which surface executes them? | Registry read/write on every cycle; rollout only in the `rollout` stage. The gateway executes both; nothing else touches either client. |
| Where is the approval boundary, and what isolates the action afterwards? | Immediately before the rollout call. Isolation: an envelope scoped to one experiment, one variant, one percentage, expiring in minutes. |
| What evidence is emitted, and what criteria judge it? | A span per cycle stage, an audit record per rollout naming the approver. Judged by the gates below. |
| **What identifies "this resource, now"?** | `(experiment_id, experiment_version, data_as_of)`, where `experiment_version` covers the targeting model, the risk threshold, `f()`, the covariate feature set and the outcome estimator — every frozen choice, in one version. This is the input `runtime/snapshot.py` has been waiting for — an approval granted against Tuesday's data does not authorize a rollout on Thursday. Trigger-based evaluation makes `data_as_of` sharper, not vaguer: it is the watermark of the batch that fired the trigger. |

---

## Boundaries

**Always**

- Route both side effects — registry write and rollout — through `gateway.execute` with an identity envelope and an idempotency key.
- Make every evaluation idempotent on `(experiment_id, data_as_of)`. The event source delivers at-least-once, and a trigger can fire twice on the same batch.
- Carry `experiment_version` and `data_as_of` on every context item, envelope and approval.
- Give every `trigger` wait a deadline. A trigger that never fires must surface, not vanish.
- Explain an abstention in Slack with the evidence that drove it.

**Ask first**

- Adding a dependency; changing a `.importlinter` contract; changing the checkpoint schema for a workflow with live runs; changing a policy threshold; changing which metric is primary; widening the rollout percentage beyond what was specced.

**Never**

- Let the evaluation's service account hold `experiments:rollout`.
- Roll out on an approval whose `(experiment_version, data_as_of)` no longer matches.
- Treat a predicted outcome as an observed one. TabPFN reduces variance; it never stands in for behaviour that has not happened.
- Compute the variance-reduction covariate from anything observed after randomisation.
- Draw the control arm from anywhere but the targeted cohort. Randomisation happens after targeting, inside the eligible population, or regression to the mean manufactures a saving that was never there.
- Let a predicted quantity enter the primary metric. Saved LTV per arm is computed from observed retention and observed revenue; the counterfactual is the control arm, not a model.
- Change the targeting model, the risk threshold, `f()`, or the covariate feature set mid-experiment. All are frozen at start and carried in `experiment_version`.
- Roll out beyond the targeted cohort. The experiment supports a claim about the population it enrolled and no one else.
- Compute the covariate from anything observed after randomisation.
- Let a `metric_movement` trigger produce a proposal. It may refresh evidence and it may trip a guardrail into an abstention. It may never advance an experiment.
- Treat an absent trigger as a quiet success — a run waiting longer than its deadline is stalled, not fine.
- Let the language model's draft text reach an authority decision. It goes in the proposal a human reads; it never becomes an argument.

---

## Success Criteria

Specific and testable. These become gate cases.

1. **Survives a restart.** A run waiting between cycles resumes on the next trigger with its state intact, from a checkpoint rebuilt in a *fresh process*.
2. **Survives a deploy.** A run that waited across a code change resumes rather than erroring, or fails loudly with a migration message — never silently diverges.
3. **A trigger delivered twice produces one evaluation.** At-least-once delivery meets an idempotent cycle keyed on `data_as_of`.
4. **A stalled run surfaces.** A `trigger` wait past its deadline is reported by `operator stalled`, not left sleeping.
5. **No rollout without a bound approval.** Approval binds `(run_id, action fingerprint, experiment_version, data_as_of)`; any mismatch refuses.
6. **A stale approval refuses.** Approval on Tuesday's batch, rollout attempted after a newer batch landed → refused, audited.
7. **The evaluation account cannot roll out.** A rollout attempted with the cycle's own envelope is denied at policy, not at approval.
8. **An unresolved rollout is never retried blind.** The existing claim/finalize semantics hold against a real API that times out.
9. **A metric-movement trigger can never propose.** Drive a cycle from each trigger kind against identical evidence that would justify a rollout: `data_arrival` proposes, `metric_movement` returns continue. One assertion, and it is the whole optional-stopping defence.
10. **The covariate is pre-treatment.** The feature window used for the covariate ends at or before the randomisation timestamp. Assertable directly from the feature spec; a covariate that reaches past assignment fails the cycle.
11. **Control comes from the targeted cohort.** Every control subject passes the same targeting predicate, at the same model version, as every treatment subject. Assertable directly from assignment records; a control drawn outside the cohort fails the cycle before any analysis runs.
12. **The primary metric reads only observed quantities.** No predicted churn, predicted LTV or predicted spend appears anywhere in the Incremental Net Saved Value computation. Assertable from the metric's input schema.
13. **A degraded covariate changes precision, not the answer.** Re-run a decided experiment with the covariate shuffled, zeroed and replaced with noise. τ stays within tolerance of the unadjusted estimate every time; only the interval widens. This also catches a post-treatment feature slipped into `X_pre`.
14. **The rollout cannot exceed the cohort.** The rollout request carries `targeting_model_version` and `risk_threshold`, both matching the experiment's frozen values; a mismatch or a missing cohort predicate refuses before execution.
15. **The approver sees a headcount.** The Slack prompt renders the estimated number of real customers affected, not a bare percentage. "10% of the targeted cohort" is not a quantity a human can weigh; "≈4,200 customers" is.
16. **A degraded targeting model narrows the claim, it does not change it.** Re-run a decided experiment with the risk scores shuffled: the estimate on whoever was enrolled stays valid, and the recorded cohort description changes. This asserts the internal/external validity split above rather than trusting it.
17. **Targeting is real and reproducible.** The same dataset snapshot and the same model version produce the same eligible cohort, and the cohort's size and risk distribution are recorded on the experiment. A cohort that cannot be reproduced cannot be rolled out to.
18. **The licence gate fails at startup, not mid-run.** A missing or invalid `TABPFN_TOKEN` is refused when the service starts. A run must never park on a wait and then discover it cannot score.
19. **A model-drafted candidate cannot bypass validation.** A proposal with a missing field, a wrong type, an extra argument, or a tool it was not exposed for is refused before the registry is touched, and the turn still answers. The existing `tool.reject` path, now driven by a real model.
20. **PRE_COMMIT is not ALWAYS.** A `PRE_COMMIT` action proceeds without a human and still produces an audit record naming the policy rule that granted it. A `ALWAYS` action without a human approval refuses. Two tiers, two behaviours, asserted.
21. **An incompatible checkpoint parks the run.** A run whose checkpoint schema predates an incompatible change enters `needs_migration`, is reported by `operator stalled`, and never resumes into a shape it does not understand.
22. **CI catches a stranding deploy.** An incompatible checkpoint schema change that ships no migration note fails the build.
23. **The end-to-end path runs.** Trigger to rollout, through a real Slack approval, with the process killed while the run is parked and restarted before the human answers. One test, and it is the iteration's definition of done.
24. **A forged approval is refused.** An interaction payload with a bad signature, a stale timestamp, or a replayed body never reaches the approval store. Asserted at the adapter, before any policy runs.
25. **An unauthorised approver is refused.** A correctly-signed interaction from a user outside the approver group is rejected in layer 8, not in the adapter — the adapter has no opinion about who may approve.
26. **Every cycle is reconstructable.** Given a run id, the full sequence of cycles, evidence bundles, verdicts, proposals and approvals can be replayed from the trace weeks later.
27. **Abstentions are legible.** A human reading the Slack message can say what evidence produced it without opening a dashboard.
28. **Budgets hold.** p95 evaluation ≤ 60s; cost per evaluation recorded and within budget.

---

## Decided during review

Recorded here so the reasoning is not lost, and so a later change to one of these is
visible as a change rather than a discovery.

| | Decision |
|---|---|
| Agent shape | one durable run per experiment, trigger-based |
| Durable execution | Temporal owns the run; LangGraph + Postgres checkpointer run the turn (`SPEC-durable-runtime.md`, ADR-0007; supersedes the LangGraph-only decision) |
| Language model's role | drafting proposals and explaining abstentions; nothing else |
| TabPFN's role | **both** targeting and adjustment |
| Primary metric | Incremental Net Saved Value |
| Guardrails | conversion, churn |
| Covariate | `eLTV_pre` composite, not the raw risk score (range restriction) |
| Rollout scope | the targeted cohort only |
| Approval timeout | re-ask, never silent expiry |
| Trigger authority | `metric_movement` may abstain, never propose |
| Candidate authorship | **the model drafts it**, and the draft goes through the full validation chain |
| Checkpoint incompatibility | **fail loud and park** — never silently diverge |
| Iteration-1 thresholds | defaulted below; change a number, not a design |
| Concurrency target | designed for hundreds, verified at 100 |
| Approver group | one per tenant; policy checks the *acting* tenant's group |
| Registry consumers | none outside this run — stays one capability |
| Registry store and tools | Postgres (`migrations/0012`) and nine narrow tools, each one thing to one experiment in one state (`SPEC-registry.md`) |

---

## Iteration-1 decisions and deferrals

### Iteration-1 decisions

All six are settled. Three you chose; three I defaulted and have marked, because each
is a number or a scoping call rather than a design, and each is cheap to change.

**1. The model drafts the candidate.** It proposes the hypothesis, the variant and the
offer parameters, and that output travels the full chain before anything is written:

```
model proposal -> exposure filter -> schema validation -> policy -> gateway -> registry
```

This is the point of iteration 1 as an *agent* rather than a workflow: untrusted model
output reaching a real side effect through every defence, while the model stays out of
both the targeting and the statistics. It also puts model-authored prose in front of a
human in Slack, which is exactly the surface `ApprovalPrompt` already defangs.

> **A gap this exposes.** `create_experiment_draft` is side-effecting and reversible,
> so `ToolSpec` requires it to declare an approval tier — but waking a human for a
> reversible draft defeats the preparation/commitment split. The tier that should fit
> is `PRE_COMMIT`, and today `require_approval` treats `PRE_COMMIT` and `ALWAYS`
> identically: three tiers declared, two behaviours implemented. That is the same
> defect class as the dead containment dimensions in ADR-0004 — a declared control
> nothing enforces. Iteration 1 gives `PRE_COMMIT` real semantics: **a policy-granted
> approval, recorded in the audit trail with the rule that granted it, no human woken.**

**2. The thresholds — defaulted.**

| | Default | Why this shape |
|---|---|---|
| Risk cut | top decile by predicted churn, within the tenant's active base | a decile is scale-free; an absolute probability would depend on TabPFN's calibration, which varies by dataset |
| Minimum cohort | 1,000 customers | a 10% rollout is then ≥100 customers, enough to be worth measuring in iteration 2 |
| Value-at-risk floor | ≥ $50,000 annualised revenue at risk | from **observed** ARPU, not predicted — the regressor is iteration 2 |

Change any number and it is one line in `experiments/`, versioned like everything else.

**3. Concurrency — defaulted to hundreds, verified at 100.** Enough to force real
connection pooling and trigger fan-out without exotic infrastructure. The number that
matters is the one in the load test, and it is cheap to raise.

**4. Checkpoint incompatibility: fail loud and park.** Every checkpoint carries a schema
version. On load, an incompatible version stops the run in a distinct
`needs_migration` state — not `stalled`, because the causes and the remedies differ —
and `operator stalled` reports both. Two consequences worth naming:

- Success criterion 2 stops being an either/or. The specified behaviour *is* the loud
  failure; silent divergence is the bug.
- A deploy that breaks compatibility should be caught before it strands anything. CI
  compares the checkpoint schema against the last released version and fails the build
  on an incompatible change that ships no migration note.

**5. Approver group — defaulted to one per tenant.** Policy checks membership of the
*acting* tenant's group, so tenant A's rollout cannot be approved by tenant B's people.
Given multi-tenancy is claimed, the permissive alternative is the wrong default.

**6. Registry consumers — defaulted to none.** Stays inside this capability. The
cheaper mistake, and Phase 0 can be redone if a dashboard appears.

### Deferred to iteration 2

Recorded so they are visible as debts rather than gaps.

| Question | Why it waits |
|---|---|
| Terminal vs repeated analysis | statistics; the runtime is indifferent |
| What "Saved LTV" is, per arm | same |
| The remaining policy numbers, and `f()` | same |
| Production subscriber data source and its cadence | the trigger carries a watermark either way; the cadence also settles whether `metric_movement` earns its place |
| The rollout credential and its identity model | envelope shape is right; storage is iteration 2 |
| Model asset and production serving stack | the drafting job is undemanding |
| TabPFN as library or service | iteration 1 uses it as a library, because dev is a laptop; the `prediction/` seam keeps the switch contained |
| PII redaction, retention, sink policy | instrumentation exists; hardening is a named debt |

### A note on when the regression is fit

"Fit linear regression at the end of the experiment" and "evaluate on every trigger" are
two different architectures, and the spec currently assumes the second.

**If the analysis is terminal** — one regression, at a pre-declared endpoint — the
optional-stopping problem disappears entirely, because there is only ever one look that
can decide anything. Triggers then serve two narrower purposes: accumulating data, and
running guardrail checks that can abstain early. `propose` happens once, at the end. The
inference rule becomes an ordinary fixed-horizon ANCOVA, the sequential machinery below
is unnecessary, and roughly a third of this spec's complexity evaporates.

**If the analysis is repeated** — a regression per evaluation, each able to propose —
then the looks are repeated looks and the note below applies.

The terminal version is simpler, more conventional, and easier to defend to whoever
asks how the decision was reached. The repeated version decides sooner, which is the
whole reason variance reduction is in the design. They are not obviously ranked; it is
a genuine choice and it belongs to you.

### A note on the inference rule (only if the analysis is repeated)

The asymmetry above makes both trigger kinds *safe*. It does not by itself make the
inference rule correct, because even `data_arrival` looks are repeated looks, and a
fixed-horizon test evaluated repeatedly reports false positives at a rate well above
its nominal one. Two ways to close that:

**Group-sequential with pre-committed analysis points.** Declare the analysis points in
advance — at planned information fractions — and spend alpha across them. Classical,
well understood, and it needs a planned sample size the experiment may not have.
Proposals may only be made *at* an analysis point, which fits the asymmetry neatly:
`data_arrival` can reach one, `metric_movement` cannot.

**Anytime-valid inference.** Confidence sequences or e-values give time-uniform
coverage: valid at every moment simultaneously, and therefore valid under any stopping
rule, including one that depends on the data. The optional-stopping hazard does not
need to be managed because it does not arise. The cost is power — the sequence is wider
than a fixed-horizon interval at the planned horizon.

**Recommended: anytime-valid, and keep the asymmetry anyway.** Anytime-valid inference
is the better architectural fit, because its validity does not depend on anyone being
disciplined about when the system looks — and in a trigger-driven system, nobody can
promise that. The power it costs is exactly what TabPFN's variance reduction buys back,
so the two decisions compose rather than fight.

Keeping the asymmetry on top is then belt-and-braces rather than necessity, and worth it
for an operational reason rather than a statistical one: a proposal that arrives because
a metric twitched reads, to the human approving it, as a system reacting to noise — even
when the inference behind it is sound.
