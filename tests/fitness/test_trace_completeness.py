"""Part 8: a trace that stops at the model call cannot explain the run."""

from __future__ import annotations

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, handle
from agentstack.observability.spans import REQUIRED_SPANS
from agentstack.runtime.run import Run

from .conftest import SCOPES, approve_and_resume


def test_a_completed_run_emits_every_required_span(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    first = handle(stack, event, scopes=SCOPES, run=run)
    approve_and_resume(stack, first, run)
    second = handle(stack, event, scopes=SCOPES, run=run)

    emitted = first.tracer.names() | second.tracer.names()
    missing = REQUIRED_SPANS - emitted
    assert not missing, f"the trace does not cross the stack; missing {sorted(missing)}"


def test_the_required_span_set_covers_the_whole_stack() -> None:
    for name in (
        "context.assemble",
        "tool.expose",
        "policy.decide",
        "approval.request",
        "execution.commit",
    ):
        assert name in REQUIRED_SPANS, f"{name} is a system-layer decision and must be traced"


def test_every_span_is_version_stamped(stack: Stack, event: InboundEvent, run: Run) -> None:
    result = handle(stack, event, scopes=SCOPES, run=run)
    for span in result.tracer.spans:
        v = span.versions
        assert v.prompt and v.model and v.tool_schema and v.policy and v.retrieval, (
            "a run that cannot be reconstructed cannot be judged later"
        )


def test_the_context_decision_is_visible_not_just_the_answer(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    result = handle(stack, event, scopes=SCOPES, run=run)
    span = next(s for s in result.tracer.spans if s.name == "context.assemble")
    assert "fingerprint" in span.attributes
    assert "dropped_out_of_scope" in span.attributes
    assert "untrusted" in span.attributes
