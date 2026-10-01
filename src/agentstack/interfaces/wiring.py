"""The composition root.

Every layer is assembled here and nowhere else, which is why the layers below can stay
ignorant of each other's concrete implementations. Swapping the in-memory stores for
the backend chosen in ADR-0002 is a change to this file and to nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio.client import Client

from agentstack.context.frozen_cohorts import FrozenCohort
from agentstack.context.items import Scope, Trust
from agentstack.context.memory import MaintenanceQueue, MemoryStore
from agentstack.context.retrieval import Candidate, StaticRetriever
from agentstack.control_plane.resolve import SessionResolver
from agentstack.control_plane.session import Session, SessionView, new_session
from agentstack.control_plane.stores import SessionStore, TranscriptStore, WorkingStateStore
from agentstack.execution.gateway import Gateway
from agentstack.execution.idempotency import IdempotencyLedger
from agentstack.execution.surfaces import PostgresRegistryClient, RecordingClient, Sandbox
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.slack import ApprovalAsk, ApprovalNotice, Notifier, RecordingNotifier
from agentstack.interfaces.slack_callback import ReplayGuard, accept
from agentstack.interfaces.triggers import parse_trigger
from agentstack.model.engine import EchoEngine
from agentstack.observability.audit import AuditSink
from agentstack.observability.spans import LoggingSink, SpanSink, VersionStamp
from agentstack.policy.approval import ApprovalStore
from agentstack.policy.approvers import ApproverDirectory
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.policy.triggers import TriggerEvent
from agentstack.runtime.approvals import ApprovalCoordinator, Resolution
from agentstack.runtime.drafting import estimated_customers
from agentstack.runtime.graph import build_turn_graph
from agentstack.runtime.loop import TurnDeps, TurnResult, run_turn
from agentstack.runtime.run import (
    ExperimentRun,
    ExperimentRunStore,
    Run,
    RunStore,
    new_run,
    new_run_id,
)
from agentstack.runtime.steps import StepLedger
from agentstack.runtime.temporal.client import deliver_trigger, notify_answer
from agentstack.runtime.temporal.contracts import TASK_QUEUE, RunStart, Trigger
from agentstack.runtime.waits import Wait, WaitStore
from agentstack.storage.database import Database
from agentstack.tools.action import ActionRequest
from agentstack.tools.catalog import build_registry
from agentstack.tools.experiments import DRAFT_STAGE, EVALUATION_STAGE, ROLLOUT_STAGE
from agentstack.tools.spec import ActsAs, Surface

VERSIONS = VersionStamp(
    prompt="support-v1",
    model="echo-0",
    tool_schema="catalog-v1",
    policy="policy-v1",
    retrieval="static-v1",
)

# How many earlier reads a turn is shown. A handful, most recent: enough to act on what
# was just read, not so many that old reads crowd out the question (list reads are
# themselves capped at 50 rows).
OBSERVATIONS_IN_CONTEXT = 5


@dataclass(slots=True)
class Stack:
    resolver: SessionResolver
    sessions: SessionStore
    runs: RunStore
    transcripts: TranscriptStore
    working_state: WorkingStateStore
    memory: MemoryStore
    maintenance: MaintenanceQueue
    approvals: ApprovalStore
    ledger: IdempotencyLedger
    audit: AuditSink
    steps: StepLedger
    waits: WaitStore
    client: RecordingClient
    registry_client: PostgresRegistryClient
    # Where an approval question goes. A recorder by default: a stack built in a
    # test must not post to a real channel, and one built for the walkthrough has
    # no workspace to post to.
    notifier: Notifier
    # Transport-level deduplication for Slack callbacks. In Postgres, because a
    # replay landing on a different worker is the case it exists for.
    replay_guard: ReplayGuard
    # Who may approve, per tenant. Consulted in layer 8, never in the adapter.
    approver_directory: ApproverDirectory
    # Turns a human's answer into an approval the gateway will accept. Commits
    # nothing itself: the next turn does, through the gateway.
    coordinator: ApprovalCoordinator
    deps: TurnDeps
    # Which run is an experiment's. A trigger names an experiment; Temporal needs a run.
    experiment_runs: ExperimentRunStore


def build_stack(
    db: Database, checkpointer: Any, *, tenant: str = "acme", traces: SpanSink | None = None
) -> Stack:
    """Assemble the stack against a migrated database.

    `db` and `checkpointer` are required rather than defaulted. A default would open a
    pool as a side effect of importing convenience, and the first thing to go wrong
    would be a test quietly writing to the dev database. The checkpointer is the same
    argument: the only sensible default is the in-memory one, and a durable-looking
    runtime silently backed by a dictionary is the failure this whole phase exists to
    remove.
    """
    sessions = SessionStore(db=db)
    transcripts = TranscriptStore(db=db)
    working_state = WorkingStateStore(db=db)
    resolver = SessionResolver(
        sessions=sessions, transcripts=transcripts, working_state=working_state
    )
    memory = MemoryStore(db=db)
    maintenance = MaintenanceQueue(db=db)
    approvals = ApprovalStore(db=db)
    ledger = IdempotencyLedger(db=db)
    audit = AuditSink(db=db)
    steps = StepLedger(db=db)
    waits = WaitStore(db=db)
    runs = RunStore(db=db)
    client = RecordingClient()
    # The registry is ours and outlives the process (T25). The API client beside it is
    # still the reference fake: the rollout credential is an iteration-2 debt.
    registry_client = PostgresRegistryClient(db=db)
    notifier = RecordingNotifier()
    replay_guard = ReplayGuard(db=db)
    approvers = ApproverDirectory(db=db)
    coordinator = ApprovalCoordinator(
        runs=runs,
        waits=waits,
        approvals=approvals,
        directory=approvers,
        audit=audit,
        traces=traces if traces is not None else LoggingSink(),
        versions=VERSIONS,
    )

    gateway = Gateway(
        surfaces={Surface.API: client, Surface.REGISTRY: registry_client},
        ledger=ledger,
        approvals=approvals,
        audit=audit,
        sandbox=Sandbox(
            tenant=tenant,
            allowed_surfaces=frozenset({Surface.API, Surface.REGISTRY}),
            allowed_resource_prefixes=frozenset({f"{tenant}/customers/", f"{tenant}/experiments/"}),
        ),
    )

    retriever = StaticRetriever(
        corpus=[
            Candidate(
                text="Rollout policy: a rollout above 10% needs a named approver.",
                score=1.0,
                source="policy-handbook",
                scope=Scope(tenant=tenant),
                observed_at=datetime.now(UTC),
                trust=Trust.FIRST_PARTY,
            )
        ]
    )

    deps = TurnDeps(
        engine=EchoEngine(),
        registry=build_registry(),
        gateway=gateway,
        retriever=retriever,
        memory=memory,
        maintenance=maintenance,
        steps=steps,
        waits=waits,
        versions=VERSIONS,
        graph=build_turn_graph(checkpointer),
    )
    return Stack(
        resolver=resolver,
        sessions=sessions,
        runs=runs,
        transcripts=transcripts,
        working_state=working_state,
        memory=memory,
        maintenance=maintenance,
        approvals=approvals,
        ledger=ledger,
        audit=audit,
        steps=steps,
        waits=waits,
        client=client,
        registry_client=registry_client,
        notifier=notifier,
        replay_guard=replay_guard,
        approver_directory=approvers,
        coordinator=coordinator,
        deps=deps,
        experiment_runs=ExperimentRunStore(db=db),
    )


def envelope_for(
    view: SessionView, *, scopes: frozenset[str], lifetime: timedelta = timedelta(minutes=15)
) -> IdentityEnvelope:
    """A per-run envelope: narrow scopes, short lifetime, revocable.

    Note that it is built from the *session view*, never from model or tool output.
    """
    return IdentityEnvelope(
        principal=view.user_id,
        acts_as=ActsAs.DELEGATED,
        tenant=view.tenant,
        delegation_scopes=scopes,
        credential_ref=f"vault://agent/{view.tenant}/{view.user_id}",
        expires_at=datetime.now(UTC) + lifetime,
        revocable=True,
    )


def handle(
    stack: Stack, event: InboundEvent, *, scopes: frozenset[str], run: Run | None = None
) -> TurnResult:
    """Channel event -> session -> run -> turn. The whole request path in one place.

    Pass `run` to continue an existing run after a wait was satisfied. A new `Run`
    would be a different execution: approvals, steps and idempotency all hang off the
    run id, and reusing the id is what makes resume mean resume.
    """
    view = stack.resolver.resolve(
        session_id=event.session_id, user_id=event.user_id, tenant=event.tenant
    )
    stack.transcripts.append(session_id=view.session_id, kind="user", body=event.text)
    run = stack.runs.ensure(
        run
        or new_run(
            session_id=view.session_id,
            tenant=view.tenant,
            user=view.user_id,
            stage=view.stage,
            channel=event.channel,
        )
    )
    earlier = stack.transcripts.recent(
        view.session_id, kind="observation", limit=OBSERVATIONS_IN_CONTEXT
    )
    result = run_turn(
        run=run,
        envelope=envelope_for(view, scopes=scopes),
        message=event.text,
        deps=stack.deps,
        observations=[(e.body, e.at) for e in earlier],
    )
    # What the turn read is part of the record before it is part of any prompt: the
    # transcript holds it, and the next turn's context is derived from there.
    for observation in result.observations:
        stack.transcripts.append(
            session_id=view.session_id,
            kind="observation",
            body=json.dumps(observation, sort_keys=True),
        )
    stack.transcripts.append(session_id=view.session_id, kind="agent", body=result.text)
    return result


# Who an experiment's session belongs to. The operator, not a customer and not whoever
# sent the trigger: the trigger's tenant is a claim, and it chooses nothing but which
# tenant's experiment is meant. Authority is minted per act, later, in an activity.
EXPERIMENT_OPERATOR = "agent-operator"


def run_for_trigger(stack: Stack, event: TriggerEvent) -> RunStart:
    """The run this trigger belongs to, created the first time an experiment is triggered.

    Claim, then write. The mapping is claimed with ids minted here, and only the
    winner's ids are then written, each with an upsert, so any number of first
    deliveries converge on one session and one run, and a delivery that died halfway
    is finished by the next one.
    """
    mapping = stack.experiment_runs.claim(
        ExperimentRun(
            tenant=event.tenant,
            experiment_id=event.experiment_id,
            run_id=new_run_id(),
            session_id=new_session(user_id=EXPERIMENT_OPERATOR, tenant=event.tenant).session_id,
        )
    )
    stack.sessions.ensure(
        Session(
            session_id=mapping.session_id,
            user_id=EXPERIMENT_OPERATOR,
            tenant=mapping.tenant,
            created_at=datetime.now(UTC),
        )
    )
    run = stack.runs.ensure(
        Run(
            run_id=mapping.run_id,
            session_id=mapping.session_id,
            tenant=mapping.tenant,
            user=EXPERIMENT_OPERATOR,
            channel="trigger",
        )
    )
    return RunStart(
        run_id=run.run_id,
        session_id=run.session_id,
        tenant=run.tenant,
        user=run.user,
        stage=run.stage,
        channel=run.channel,
    )


async def deliver(
    stack: Stack,
    client: Client,
    payload: dict[str, Any],
    *,
    source: str,
    task_queue: str = TASK_QUEUE,
) -> str:
    """Trigger ingress -> run -> workflow. Returns the run it was handed to.

    Parsing refuses a malformed trigger before anything is written. The run is resolved
    from the record, and the workflow is only told that something happened.
    """
    event = parse_trigger(payload, source=source)
    start = run_for_trigger(stack, event)
    trigger = Trigger(
        kind=event.kind.value,
        experiment_id=event.experiment_id,
        data_as_of=event.data_as_of,
        tenant=event.tenant,
        source=event.source,
    )
    await deliver_trigger(client, start, trigger, task_queue=task_queue)
    return start.run_id


# The scopes each stage's turn is given. A drafting turn can write a draft and cannot
# roll anything out, whatever it is shown or asks for: least privilege per act. An
# evaluation turn may abstain or halt - divergent branches from one trigger, not a
# sequence - so it carries both, never a scope from a stage it is not in.
STAGE_SCOPES: dict[str, frozenset[str]] = {
    DRAFT_STAGE: frozenset({"experiments:draft"}),
    ROLLOUT_STAGE: frozenset({"experiments:rollout"}),
    EVALUATION_STAGE: frozenset({"experiments:annotate", "experiments:halt"}),
}


@dataclass(slots=True)
class ExperimentTurns:
    """The turn host for an experiment run's worker (`runtime.temporal.TurnHost`).

    Session work stays here, above the runtime, as it does in `handle`: the session is
    resolved and the envelope minted inside the activity, for this turn, from the run's
    own record. A stage with no scopes listed gets none.
    """

    stack: Stack

    @property
    def deps(self) -> TurnDeps:
        return self.stack.deps

    def envelope(self, run: Run) -> IdentityEnvelope:
        view = self.stack.resolver.resolve(
            session_id=run.session_id, user_id=run.user, tenant=run.tenant, stage=run.stage
        )
        return envelope_for(view, scopes=STAGE_SCOPES.get(run.stage, frozenset()))

    def record(self, run: Run, *, asked: str, answered: str) -> None:
        self.stack.transcripts.append(session_id=run.session_id, kind="user", body=asked)
        self.stack.transcripts.append(session_id=run.session_id, kind="agent", body=answered)


# What the approver is told when the run stops short of the act they answered. Each says
# that nothing was committed: silence after a "yes" reads as "done".
WITHDRAWN_TEXT: dict[str, str] = {
    "stale": (
        "Approved: {summary}\nThe draft changed after you approved it, so nothing was "
        "committed. It will not roll out unless it is proposed and approved again."
    ),
    "superseded": (
        "Withdrawn: {summary}\nThe draft changed while this was waiting for an answer, so "
        "the question is closed and nothing was committed. Answering it now does nothing."
    ),
}


@dataclass(frozen=True, slots=True)
class ChannelAsker:
    """The asker for an experiment run's worker (`runtime.temporal.Asker`).

    Builds the question from the record: what the wait asks about (its summary, bound
    to the fingerprint and snapshot it was parked against, and the action it holds) and
    what the frozen cohort says it touches. The headline's percentage, and so its
    headcount, are the held action's: the payload that commits if the answer is yes, not
    a constant that happens to agree with it (A1, C1). The channel is the stack's
    notifier: Slack in production, a recorder in tests.
    """

    notifier: Notifier
    channel: str = "#experiments"

    def ask(self, *, run: Run, wait: Wait, cohort: FrozenCohort, action: ActionRequest) -> str:
        if wait.approval_summary is None:
            raise ValueError(f"{wait.wait_id} does not say what it asks about; nothing to put")
        percentage = int(action.payload["percentage"])
        return self.notifier.post_approval(
            ApprovalAsk(
                run_id=run.run_id,
                wait_id=wait.wait_id,
                summary=wait.approval_summary,
                experiment_version=str(action.payload["experiment_version"]),
                data_as_of=cohort.data_as_of,
                percentage=percentage,
                estimated_customers=estimated_customers(cohort, percentage),
                annual_value_at_risk_cents=cohort.annual_value_at_risk_cents,
                tenant=run.tenant,
                currency=cohort.currency,
            ),
            channel=self.channel,
        )

    def withdraw(self, *, run: Run, wait: Wait, reason: str) -> str:
        if wait.approval_summary is None:
            raise ValueError(f"{wait.wait_id} does not say what it asks about; nothing to say")
        return self.notifier.post_notice(
            ApprovalNotice(
                run_id=run.run_id,
                wait_id=wait.wait_id,
                tenant=run.tenant,
                reason=reason,
                text=WITHDRAWN_TEXT[reason].format(summary=wait.approval_summary),
            ),
            channel=self.channel,
        )


async def answer(
    stack: Stack,
    client: Client,
    *,
    raw_body: bytes,
    sent_at: str,
    signature: str,
    secret: str | None = None,
    now: float | None = None,
) -> Resolution:
    """Slack's answer -> authorised and recorded -> the run woken. In that order.

    `accept` proves the request is Slack's, fresh, and the first of its kind (layer 1).
    `coordinator.apply` decides whether this person may answer for the run's tenant, and
    records the approval (or refusal) against the wait (layer 8, ADR-0008 rule 4). Only
    then is the workflow told, by a signal that carries ids and grants nothing. Anyone
    refused on the way is refused before the run hears anything.
    """
    reply = accept(
        raw_body=raw_body,
        sent_at=sent_at,
        signature=signature,
        guard=stack.replay_guard,
        secret=secret,
        now=now,
    )
    resolution = stack.coordinator.apply(reply)
    await notify_answer(client, run_id=resolution.run_id, wait_id=resolution.wait_id)
    return resolution
