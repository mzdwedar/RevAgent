"""T49: traces cross the workflow and its activities, and are never history or audit.

`test_trace_completeness` proves the required spans for a turn run by `handle`, which
hands its tracer back to its caller. An activity's caller is the workflow, which must
never hold spans: what it holds is written into history (T48). So the worker's
activities export their spans to a sink, and this reads that sink after a real run
through the production worker: proposed, drafted, parked, asked, approved in Slack and
committed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from agentstack.interfaces.wiring import Stack, answer
from agentstack.observability.spans import REQUIRED_SPANS, CollectingSink, LoggingSink, Span
from agentstack.runtime.temporal.contracts import RunProgress, workflow_id
from agentstack.storage.database import Database
from tests.durability.test_approval_wait import propose_and_wait
from tests.durability.test_approval_wait import stack as stack  # the same fixture
from tests.durability.test_commit import acted
from tests.durability.test_slack_answer import APPROVER, slack_click

pytestmark = pytest.mark.usefixtures("fixture_dataset")


@pytest.fixture(autouse=True)
def _approver(stack: Stack) -> None:
    stack.approver_directory.add(
        tenant="acme", slack_user_id=APPROVER, principal="ana@acme", added_by="t49"
    )


def test_a_run_through_the_worker_is_traced_across_every_layer(
    stack: Stack, app_database: Database
) -> None:
    sink = CollectingSink()
    temporal: dict[str, str] = {}

    async def approve(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        temporal["run"] = (await handle.describe()).run_id
        await answer(stack, env.client, **slack_click(parked))
        return await acted(handle)

    run_id, _, done = propose_and_wait(stack, app_database, then=approve, traces=sink)
    assert done.commits[0].status == "committed"

    spans = sink.spans
    missing = REQUIRED_SPANS - {span.name for span in spans}
    assert not missing, f"the worker's trace does not cross the stack; missing {sorted(missing)}"

    # Our identity, never Temporal's: the run and its session, as the record has them.
    run = stack.runs.get(run_id)
    assert run is not None
    assert {(span.run_id, span.session_id) for span in spans} == {(run_id, run.session_id)}
    for foreign in (workflow_id(run_id), temporal["run"]):
        assert all(foreign not in json.dumps(span.attributes, default=str) for span in spans)

    # The act is traced where it happened. Only the commit activity commits the rollout,
    # so its commit span can have come from nowhere else.
    committed = {s.attributes.get("tool") for s in spans if s.name == "execution.commit"}
    assert committed == {"create_experiment_draft", "roll_out_variant_to_percentage"}
    assert len(observed_rollout(spans)) == 2, "read when parked, and again at the act"


def observed_rollout(spans: list[Span]) -> list[Span]:
    return [
        s
        for s in spans
        if s.name == "execution.observe" and str(s.attributes["resource"]).endswith("/rollout")
    ]


def test_a_refused_act_is_traced_too(stack: Stack, app_database: Database) -> None:
    """The commit exports its spans however it ends. A refusal is the trace someone
    will ask for, and the one an exporter-on-success would lose."""
    sink = CollectingSink()

    async def refuse(env: Any, handle: Any, parked: RunProgress) -> RunProgress:
        await answer(stack, env.client, **slack_click(parked, approve=False))
        return await acted(handle)

    run_id, _, done = propose_and_wait(stack, app_database, then=refuse, traces=sink)

    assert done.commits[0].refusal == "ApprovalRequired"
    # The rollout turn reads the world once, when it parks; the act reads it again. The
    # second read is the commit activity's, and only an export on refusal keeps it.
    assert len(observed_rollout(sink.for_run(run_id))) == 2, "the refused act left no trace"
    audit = [r for r in stack.audit.for_run(run_id) if r.resource.endswith("/rollout")]
    assert audit[-1].outcome == "refused", "and it is audited, in Postgres, separately"


def test_the_logging_sink_writes_one_json_line_per_span(caplog: pytest.LogCaptureFixture) -> None:
    from agentstack.interfaces.wiring import VERSIONS

    span = Span(
        name="execution.commit",
        run_id="run-1",
        session_id="sess-1",
        versions=VERSIONS,
        attributes={"tool": "roll_out_variant_to_percentage", "deduplicated": False},
    )
    with caplog.at_level(logging.INFO, logger="agentstack.traces"):
        LoggingSink().export([span, span])

    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "agentstack.traces"]
    assert len(lines) == 2
    assert lines[0]["span"] == "execution.commit"
    assert (lines[0]["run_id"], lines[0]["session_id"]) == ("run-1", "sess-1")
    assert lines[0]["attributes"]["tool"] == "roll_out_variant_to_percentage"
    assert lines[0]["versions"]["model"] == VERSIONS.model
