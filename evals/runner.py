"""Run the gate cases against the real stack.

Deterministic assertions, not a model grader. A model grader is the right tool for
"was the tone appropriate"; it is the wrong tool for "was the refund approved before
it was issued", which has an answer.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentstack.context.items import Scope, Trust
from agentstack.context.retrieval import Candidate, StaticRetriever
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.runtime.run import new_run
from agentstack.runtime.waits import ResumeEvent, resume

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
    expect_error: str | None = None
    description: str = ""

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


def _stack_for(case: Case) -> Stack:
    stack = build_stack(tenant=TENANT)
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


def run_case(case: Case) -> Outcome:
    started = time.perf_counter()
    failures: list[str] = []
    stack = _stack_for(case)
    session = stack.resolver.start(user_id=USER, tenant=TENANT)
    run = new_run(session_id=session.session_id, tenant=TENANT, user=USER, channel="eval")
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
            resume(
                stack.waits,
                ResumeEvent(
                    run_id=run.run_id,
                    wait_id=result.pending_wait.wait_id,
                    state_snapshot=result.pending_wait.state_snapshot,
                    payload={},
                ),
            )
            for _ in range(case.repeat):
                result = handle(stack, event, scopes=scopes, run=run)
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
    if "audit_records" in expect and len(stack.audit.records) != expect["audit_records"]:
        failures.append(f"{len(stack.audit.records)} audit records != {expect['audit_records']}")
    if "audit_outcomes" in expect:
        outcomes = sorted({r.outcome for r in stack.audit.records})
        if outcomes != sorted(expect["audit_outcomes"]):
            failures.append(f"audit outcomes {outcomes} != {sorted(expect['audit_outcomes'])}")

    seconds = time.perf_counter() - started
    budget = expect.get("max_seconds")
    if budget is not None and seconds > float(budget):
        failures.append(f"took {seconds:.2f}s, budget {budget}s")

    return Outcome(case=case, passed=not failures, failures=failures, seconds=seconds)


def load_cases() -> list[Case]:
    return sorted((Case.load(p) for p in CASES_DIR.glob("*.json")), key=lambda c: (c.part, c.id))
