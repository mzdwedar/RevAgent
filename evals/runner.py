"""Run the gate cases against the real stack.

Deterministic assertions, not a model grader. A model grader is the right tool for
"was the tone appropriate"; it is the wrong tool for "was the rollout approved before
it went out", which has an answer.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentstack.context.frozen_cohorts import FrozenCohort, FrozenCohortStore
from agentstack.context.items import Scope, Trust
from agentstack.context.retrieval import Candidate, StaticRetriever
from agentstack.context.targeting import Cohort, TargetingRule
from agentstack.execution.surfaces import PostgresRegistryClient, RegistryClient
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.slack import RecordingNotifier
from agentstack.interfaces.triggers import parse_trigger
from agentstack.interfaces.wiring import ExperimentTurns, Stack, build_stack, envelope_for, handle
from agentstack.observability.spans import CollectingSink
from agentstack.policy.triggers import Outcome as Outcome_
from agentstack.policy.triggers import OutcomeNotAuthorized
from agentstack.runtime.cycles import CycleStore, evaluate
from agentstack.runtime.drafting import rollout_admission
from agentstack.runtime.loop import run_turn
from agentstack.runtime.run import Run, new_run
from agentstack.runtime.steps import StepLedger
from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.contracts import CommitIntent
from agentstack.runtime.waits import HUMAN_APPROVAL, ResumeEvent, resume
from agentstack.storage.database import Database
from agentstack.storage.provision import truncate_all
from agentstack.tools.experiments import ROLLOUT_STAGE
from agentstack.tools.spec import Surface

CASES_DIR = Path(__file__).resolve().parent / "cases"
TENANT = "acme"
USER = "agent-operator"


@dataclass(frozen=True)
class Case:
    id: str
    part: int
    gate: bool
    message: str
    scopes: list[str]
    expect: dict[str, Any]
    approve: bool = False
    repeat: int = 1
    corpus: list[str] = field(default_factory=list)
    # Make the surface apply the effect and then lose the answer, from the first
    # turn after approval. The failure the idempotency design exists for.
    fail_after_effect: bool = False
    # Grant the approval but never satisfy the wait. The run must stay blocked.
    resume: bool = True
    expect_error: str | None = None
    # A trigger case exercises the ingress instead of a turn: the same trigger is
    # delivered `deliveries` times and the cycle count is what is asserted.
    trigger: dict[str, Any] | None = None
    deliveries: int = 1
    # A rollout case runs the rollout turn against this frozen cohort, held to it as the
    # worker holds it, with `message` as what the model proposes (A1).
    frozen_cohort: dict[str, Any] | None = None
    # Then wake the run's act while its approval wait is still unanswered (A1, H2).
    wake_unanswered: bool = False
    # The stage the run is on, which decides the tools its turn is shown.
    stage: str = "default"
    # The process dies after the surface applied the effect and the ledger settled it,
    # before the step recorded it. The same turn is then run again by a fresh stack.
    dies_before_step_completes: bool = False
    description: str = ""
    # The stage the run is on, which decides its menu.
    stage: str = "default"
    # Run against the in-memory registry, the fake held to the same contract as Postgres,
    # so the surface can lose an answer (`fail_after_effect`) and its reads can be counted.
    fake_registry: bool = False
    # What the registry already holds, written before the turn as an earlier run would
    # have: [{"resource": ..., "payload": ...}], committed in order.
    registry: list[dict[str, Any]] = field(default_factory=list)
    # Later turns of the same run, so what one turn read is in the next one's context.
    followups: list[str] = field(default_factory=list)
    # The experiment the run is about, as the trigger that woke it would have said.
    # Required on the evaluation stage, where it is what bounds the run's writes.
    subject: str | None = None

    @staticmethod
    def load(path: Path) -> Case:
        raw = json.loads(path.read_text())
        return Case(**raw)


@dataclass
class Outcome:
    case: Case
    passed: bool
    failures: list[str]
    seconds: float


def _stack_for(case: Case, db: Database, checkpointer: Any) -> Stack:
    # Each case starts from an empty substrate. Idempotency keys are stable across
    # runs by design, so two cases rolling out the same experiment would otherwise share
    # one - and the second would deduplicate against the first.
    truncate_all(db)
    stack = build_stack(db, checkpointer, tenant=TENANT)
    if case.corpus:
        stack.deps.retriever = StaticRetriever(
            corpus=[
                Candidate(
                    text=text,
                    score=1.0,
                    source="eval-corpus",
                    scope=Scope(tenant=TENANT),
                    observed_at=datetime.now(UTC),
                    trust=Trust.UNTRUSTED,
                )
                for text in case.corpus
            ]
        )
    return stack


def _run_trigger_case(case: Case, db: Database, started: float) -> Outcome:
    """Deliver one trigger `deliveries` times and count the evaluations it caused."""
    failures: list[str] = []
    truncate_all(db)
    store = CycleStore(db=db)
    assert case.trigger is not None
    event = parse_trigger(case.trigger, source="eval")
    evaluations: list[str] = []

    def evaluator(_: Any) -> tuple[Outcome_, str | None]:
        evaluations.append("evaluated")
        return Outcome_(str(case.expect.get("outcome", "continue"))), None

    refused: str | None = None
    for _ in range(case.deliveries):
        try:
            evaluate(store, event, evaluator)
        except OutcomeNotAuthorized as exc:
            refused = str(exc)

    expected = int(case.expect.get("evaluations", 1))
    if len(evaluations) != expected:
        failures.append(f"{len(evaluations)} evaluation(s) != {expected}")
    if case.expect_error and (refused is None or case.expect_error not in refused):
        failures.append(f"expected a refusal mentioning {case.expect_error!r}, got {refused!r}")
    if not case.expect_error and refused is not None:
        failures.append(f"unexpected refusal: {refused}")

    return Outcome(
        case=case, passed=not failures, failures=failures, seconds=time.perf_counter() - started
    )


def _freeze(db: Database, spec: dict[str, Any]) -> FrozenCohort:
    """Record the frozen cohort a rollout case is about, as a propose cycle records it."""
    rule = TargetingRule(
        profile="eval", risk_quantile=0.9, minimum_cohort=1, minimum_annual_value_at_risk_cents=1
    )
    return FrozenCohortStore(db=db).record(
        tenant=TENANT,
        experiment_id=str(spec["experiment_id"]),
        cohort=Cohort(
            experiment_version=str(spec["experiment_version"]),
            dataset="eval",
            data_as_of=str(spec["data_as_of"]),
            model_version=str(spec["targeting_model_version"]),
            rule=rule,
            risk_threshold=float(spec["risk_threshold"]),
            members=tuple(range(int(spec["size"]))),
            annual_value_at_risk_cents=int(spec["annual_value_at_risk_cents"]),
            risk_quantiles=(0.1, 0.3, 0.5, 0.7, 0.9),
            revenue_note="eval",
        ),
    )


def _run_rollout_case(case: Case, db: Database, checkpointer: Any, started: float) -> Outcome:
    """The rollout turn, held to its frozen cohort exactly as the worker holds it; then,
    if asked, the act woken while nobody has answered."""
    failures: list[str] = []
    assert case.frozen_cohort is not None
    stack = _stack_for(case, db, checkpointer)
    cohort = _freeze(db, case.frozen_cohort)
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    run = stack.runs.ensure(
        new_run(
            session_id=session.session_id,
            tenant=TENANT,
            user=USER,
            stage=ROLLOUT_STAGE,
            channel="eval",
        )
    )
    turns = ExperimentTurns(stack)
    envelope = turns.envelope(run)
    # The registry's own compare-and-set key, as the worker reads it; an experiment it has
    # no events for has none, so 0.
    history = stack.deps.gateway.surfaces[Surface.REGISTRY].read(
        f"{TENANT}/experiments/{case.frozen_cohort['experiment_id']}/history", {}
    )
    result = run_turn(
        run=run,
        envelope=envelope,
        message=case.message,
        deps=stack.deps,
        admit=rollout_admission(
            cohort=cohort,
            audit=stack.audit,
            run=run,
            principal=envelope.principal,
            prior_rollout_event=int(history.get("latest_rollout_event", 0)),
        ),
    )

    commit_status: str | None = None
    if case.wake_unanswered and result.pending_wait is not None:
        activities = RunActivities(
            runs=stack.runs,
            cycles=CycleStore(db=db),
            cohorts=FrozenCohortStore(db=db),
            waits=stack.waits,
            scorer=_NoScoring(),
            trigger_deadline=timedelta(days=1),
            traces=CollectingSink(),
            versions=stack.deps.versions,
            audit=stack.audit,
            turns=turns,
        )
        try:
            commit_status = activities.commit(
                CommitIntent(
                    run_id=run.run_id,
                    wait_id=result.pending_wait.wait_id,
                    experiment_id=cohort.experiment_id,
                    data_as_of=cohort.data_as_of,
                    kind="data_arrival",
                )
            ).status
        except Exception as exc:  # a refusal at the act is the result under test
            commit_status = type(exc).__name__

    expect = case.expect
    approval_waits = [
        w for w in stack.waits.pending_for(run.run_id) if w.kind == HUMAN_APPROVAL
    ] + [w for w in stack.waits.satisfied_for(run.run_id) if w.kind == HUMAN_APPROVAL]
    decisions = sorted({r.policy_decision for r in stack.audit.for_run(run.run_id)})
    notifier = stack.notifier
    posted = len(notifier.posted) if isinstance(notifier, RecordingNotifier) else 0
    checks: dict[str, Any] = {
        "status": result.status,
        "rollouts": len(stack.registry_client.rollouts),
        "approval_waits": len(approval_waits),
        "pending_approval_waits": len([w for w in approval_waits if not w.satisfied]),
        "questions_posted": posted,
        "audit_decisions": decisions,
        "commit_status": commit_status,
    }
    for key, actual in checks.items():
        if key in expect and actual != expect[key]:
            failures.append(f"{key} {actual!r} != {expect[key]!r}")

    return Outcome(
        case=case, passed=not failures, failures=failures, seconds=time.perf_counter() - started
    )


class _NoScoring:
    """A rollout case never evaluates a trigger."""

    model_version = "none"

    def score(self, **_: Any) -> Any:
        raise AssertionError("a rollout case scores nothing")


class ProcessDied(RuntimeError):
    """Stands in for the process ending between the effect and the step's record."""


class DiesBeforeCompleting(StepLedger):
    """A step ledger whose process dies as it goes to record a completed step."""

    def _write(self, run_id: str, name: str, status: str, receipt: str | None) -> Any:
        if status == "completed":
            raise ProcessDied(f"the process died before {name} was recorded as complete")
        return StepLedger._write(self, run_id, name, status, receipt)


def _run_rerun_case(case: Case, db: Database, checkpointer: Any, started: float) -> Outcome:
    """One turn whose process dies in the window after its effect, then the same turn
    again in a fresh stack: the rerun an at-least-once runtime makes.

    What is judged is the rerun: it must deduplicate against the ledger and finish, not
    park a question about an act that already happened (audit finding H1).
    """
    failures: list[str] = []
    first = _stack_for(case, db, checkpointer)
    session = first.resolver.start(user_id=USER, tenant=TENANT)
    run = first.runs.ensure(
        new_run(
            session_id=session.session_id,
            tenant=TENANT,
            user=USER,
            stage=case.stage,
            channel="eval",
        )
    )
    first.deps.steps = DiesBeforeCompleting(db=db)
    died = False
    try:
        _take_the_turn(first, run, case)
    except ProcessDied:
        died = True
    if not died:
        failures.append("the first attempt never reached the window it was meant to die in")

    rerun = build_stack(db, checkpointer, tenant=TENANT)
    result = _take_the_turn(rerun, run, case)
    expect = case.expect
    if "status" in expect and result.status != expect["status"]:
        failures.append(f"status {result.status!r} != {expect['status']!r}")
    drafts = len(rerun.registry_client.drafts)
    if "registry_drafts" in expect and drafts != int(expect["registry_drafts"]):
        failures.append(f"{drafts} drafts != {expect['registry_drafts']}")
    if "pending_waits" in expect:
        pending = [w.kind for w in rerun.waits.pending_for(run.run_id)]
        if len(pending) != int(expect["pending_waits"]):
            failures.append(f"pending waits {pending}, expected {expect['pending_waits']}")
    if "audit_outcomes" in expect:
        outcomes = [r.outcome for r in rerun.audit.for_run(run.run_id)]
        if outcomes != list(expect["audit_outcomes"]):
            failures.append(f"audit outcomes {outcomes} != {expect['audit_outcomes']}")
    return Outcome(
        case=case, passed=not failures, failures=failures, seconds=time.perf_counter() - started
    )


def _take_the_turn(stack: Stack, run: Run, case: Case) -> Any:
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    return run_turn(
        run=run,
        envelope=envelope_for(view, scopes=frozenset(case.scopes)),
        message=case.message,
        deps=stack.deps,
        turn_id=f"eval:{case.id}",
    )


def run_case(case: Case, db: Database, checkpointer: Any) -> Outcome:
    started = time.perf_counter()
    if case.trigger is not None:
        return _run_trigger_case(case, db, started)
    if case.frozen_cohort is not None:
        return _run_rollout_case(case, db, checkpointer, started)
    if case.dies_before_step_completes:
        return _run_rerun_case(case, db, checkpointer, started)
    failures: list[str] = []
    stack = _stack_for(case, db, checkpointer)
    registry: RegistryClient | PostgresRegistryClient = stack.registry_client
    if case.fake_registry:
        registry = RegistryClient()
        stack.deps.gateway.surfaces[Surface.REGISTRY] = registry
    for seed in case.registry:
        registry.commit(seed["resource"], seed["payload"])
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    run = new_run(
        session_id=session.session_id,
        tenant=TENANT,
        user=USER,
        stage=case.stage,
        channel="eval",
        subject=case.subject,
    )
    event = InboundEvent(
        channel="eval",
        tenant=TENANT,
        user_id=USER,
        session_id=session.session_id,
        text=case.message,
    )
    scopes = frozenset(case.scopes)

    result = None
    error: Exception | None = None
    span_names: set[str] = set()
    try:
        result = handle(stack, event, scopes=scopes, run=run)
        span_names |= result.tracer.names()
        if (
            case.approve
            and result.pending_wait
            and result.pending_request
            and result.approval_summary
        ):
            stack.approvals.grant(
                run_id=run.run_id,
                request=result.pending_request,
                state_snapshot=result.pending_wait.state_snapshot,
                approver="eval-approver",
                summary=result.approval_summary,
            )
            if case.resume:
                resume(
                    stack.waits,
                    ResumeEvent(
                        run_id=run.run_id,
                        wait_id=result.pending_wait.wait_id,
                        state_snapshot=result.pending_wait.state_snapshot,
                        payload={"approved_by": "eval-approver"},
                    ),
                )
            if case.fail_after_effect:
                assert isinstance(registry, RegistryClient), "a lost answer needs fake_registry"
                registry.fail_after_effect = True
            for _ in range(case.repeat):
                result = handle(stack, event, scopes=scopes, run=run)
                span_names |= result.tracer.names()
        for text in case.followups:
            result = handle(stack, replace(event, text=text), scopes=scopes, run=run)
            span_names |= result.tracer.names()
    except Exception as exc:  # the refusal is the result under test
        error = exc

    if case.expect_error:
        if error is None or type(error).__name__ != case.expect_error:
            failures.append(
                f"expected {case.expect_error}, got {type(error).__name__ if error else 'no error'}"
            )
    elif error is not None:
        failures.append(f"unexpected {type(error).__name__}: {error}")

    expect = case.expect
    if result is not None and error is None:
        if "status" in expect and result.status != expect["status"]:
            failures.append(f"status {result.status!r} != {expect['status']!r}")
        if "tools_exposed" in expect:
            span = next((s for s in result.tracer.spans if s.name == "tool.expose"), None)
            actual = sorted(span.attributes["tools"]) if span else []
            if actual != sorted(expect["tools_exposed"]):
                failures.append(f"tools exposed {actual} != {sorted(expect['tools_exposed'])}")
        if "spans_include" in expect:
            missing = set(expect["spans_include"]) - span_names
            if missing:
                failures.append(f"missing spans {sorted(missing)}")
        if "context_mentions" in expect:
            rendered = result.bundle.render()
            for needle in expect["context_mentions"]:
                if needle.lower() not in rendered.lower():
                    failures.append(f"context is missing {needle!r}")
        if "untrusted_items_min" in expect:
            count = len(result.bundle.untrusted())
            if count < expect["untrusted_items_min"]:
                failures.append(
                    f"{count} untrusted items, expected >= {expect['untrusted_items_min']}"
                )

    if "surface_calls" in expect and len(stack.client.calls) != expect["surface_calls"]:
        failures.append(f"{len(stack.client.calls)} surface calls != {expect['surface_calls']}")
    if "rollouts" in expect and len(registry.rollouts) != expect["rollouts"]:
        failures.append(f"{len(registry.rollouts)} rollouts != {expect['rollouts']}")
    if "surface_reads" in expect:
        reads = registry.reads if isinstance(registry, RegistryClient) else stack.client.reads
        if len(reads) != expect["surface_reads"]:
            failures.append(f"{len(reads)} surface reads != {expect['surface_reads']}")
    if "registry_events" in expect:
        # Rollouts, halts, discards and abstentions: every registry effect after a draft.
        row = db.fetch_one("SELECT count(*) FROM registry_events")
        events = int(row[0]) if row else 0
        if events != expect["registry_events"]:
            failures.append(f"{events} registry events != {expect['registry_events']}")
    # Scoped to this run, not to the sink. The audit sink is a shared table now, and
    # "every record ever written" would make each case's expectation depend on which
    # cases ran before it.
    audited = stack.audit.for_run(run.run_id)
    if "audit_records" in expect and len(audited) != expect["audit_records"]:
        failures.append(f"{len(audited)} audit records != {expect['audit_records']}")
    if "audit_outcomes" in expect:
        outcomes = sorted({r.outcome for r in audited})
        if outcomes != sorted(expect["audit_outcomes"]):
            failures.append(f"audit outcomes {outcomes} != {sorted(expect['audit_outcomes'])}")
    if "audit_decisions" in expect:
        # Which rule stopped it, not only that something did: "denied" by the tenant
        # boundary and "denied" by the subject boundary are different defences.
        decisions = sorted({r.policy_decision for r in audited})
        if decisions != sorted(expect["audit_decisions"]):
            failures.append(f"audit decisions {decisions} != {sorted(expect['audit_decisions'])}")
    for experiment, status in expect.get("experiment_status", {}).items():
        actual = stack.registry_client.read(f"{TENANT}/experiments/{experiment}", {})["status"]
        if actual != status:
            failures.append(f"{experiment} is {actual!r}, expected {status!r}")
    for experiment, exposure in expect.get("current_exposure", {}).items():
        # What customers are exposed to now, read from the store - not from the run's
        # own report, which is what said "success" while a ramp-down was dropped (C2).
        history = stack.registry_client.read(f"{TENANT}/experiments/{experiment}/history", {})
        if history.get("current_exposure") != exposure:
            failures.append(
                f"{experiment} is exposed at {history.get('current_exposure')!r}%, "
                f"expected {exposure}%"
            )

    seconds = time.perf_counter() - started
    budget = expect.get("max_seconds")
    if budget is not None and seconds > float(budget):
        failures.append(f"took {seconds:.2f}s, budget {budget}s")

    return Outcome(case=case, passed=not failures, failures=failures, seconds=seconds)


def load_cases() -> list[Case]:
    return sorted((Case.load(p) for p in CASES_DIR.glob("*.json")), key=lambda c: (c.part, c.id))
