"""Parts 6 and 7: a read and a commit are not the same action, so they are not one verb.

The surface used to expose only `commit() -> str`. Two things followed. A read had no
way to return what it read - `ExecutionResult.receipt` is an opaque string - so the
path of least resistance for the next person was to let the tool function fetch it
directly, which is exactly the capability/execution collapse contract 2 exists to
prevent: the contract would hold while the invariant it protects was breached in
shape. And `commit` would have had to branch GET-vs-POST off a resource string
assembled from model-supplied arguments.
"""

from __future__ import annotations

import pytest

from agentstack.execution.surfaces import RecordingClient, SurfaceClient
from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, envelope_for, handle
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import Run, new_run
from agentstack.tools.experiments import GET, ROLLOUT_STAGE

from .conftest import READ_SCOPES, TENANT, USER, FlakyRegistry

LOOK = "get_experiment tenant=acme experiment_id=exp-7"


def _read_event(run: Run) -> InboundEvent:
    return InboundEvent(
        channel="test", tenant=TENANT, user_id=USER, session_id=run.session_id, text=LOOK
    )


def test_the_surface_protocol_has_both_verbs() -> None:
    assert hasattr(SurfaceClient, "read")
    assert hasattr(SurfaceClient, "commit")
    client: SurfaceClient = RecordingClient()
    assert isinstance(client.read("acme/experiments/exp-1", {}), dict)


@pytest.mark.usefixtures("flaky_registry")
def test_a_read_returns_data_not_an_opaque_receipt(stack: Stack, run: Run) -> None:
    exposed = stack.deps.registry.expose_for(tenant=TENANT, stage=ROLLOUT_STAGE)
    request = stack.deps.registry.prepare(
        GET.name, {"tenant": TENANT, "experiment_id": "exp-7"}, exposed=exposed
    )
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)
    result = stack.deps.gateway.read(
        request=request,
        spec=GET,
        envelope=envelope_for(view, scopes=READ_SCOPES),
        run_id=run.run_id,
        state_snapshot="s",
        tracer=tracer,
    )
    assert result.data, "a read that cannot return what it read invites the collapse"
    assert "execution.read" in tracer.names()
    assert "execution.commit" not in tracer.names()


def test_a_read_is_never_served_from_the_idempotency_ledger(
    stack: Stack, run: Run, flaky_registry: FlakyRegistry
) -> None:
    """The agent reads exp-7, rolls it out, reads exp-7 again to confirm the exposure."""
    handle(stack, _read_event(run), scopes=READ_SCOPES, run=run)
    first = len(flaky_registry.reads)

    later = new_run(
        session_id=run.session_id, tenant=TENANT, user=USER, stage=ROLLOUT_STAGE, channel="test"
    )
    handle(stack, _read_event(later), scopes=READ_SCOPES, run=later)

    assert len(flaky_registry.reads) == first + 1, (
        "the second read was served from the ledger; idempotency is a property of "
        "effects, and a permanent cross-run read cache is not an idempotency ledger"
    )
    assert len(stack.ledger) == 0, "a read has no business in the effect ledger"


def test_the_gateway_refuses_to_commit_something_that_declares_itself_a_read(
    stack: Stack, run: Run
) -> None:
    exposed = stack.deps.registry.expose_for(tenant=TENANT, stage=ROLLOUT_STAGE)
    request = stack.deps.registry.prepare(
        GET.name, {"tenant": TENANT, "experiment_id": "exp-7"}, exposed=exposed
    )
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)
    with pytest.raises(ValueError, match="side-effecting"):
        stack.deps.gateway.execute(
            request=request,
            spec=GET,
            envelope=envelope_for(view, scopes=READ_SCOPES),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=tracer,
        )


def test_a_read_still_passes_policy_and_containment(
    stack: Stack, run: Run, flaky_registry: FlakyRegistry
) -> None:
    import dataclasses

    from agentstack.execution.surfaces import Sandbox, SandboxViolation
    from agentstack.tools.action import ActionRequest
    from agentstack.tools.spec import Surface

    request = ActionRequest(
        tool=GET.name,
        surface=Surface.REGISTRY,
        resource=f"{TENANT}/experiments/exp-7",
        payload={},
        idempotency_key="n/a",
    )
    # A run scoped to exp-9 alone: exp-7 is a well-formed read in the same tenant, so
    # nothing but containment stands between it and the registry.
    scoped = dataclasses.replace(
        stack.deps.gateway,
        sandbox=Sandbox(
            tenant=TENANT,
            allowed_surfaces=frozenset({Surface.REGISTRY}),
            allowed_resource_prefixes=frozenset({f"{TENANT}/experiments/exp-9/"}),
        ),
    )
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=stack.deps.versions)
    with pytest.raises(SandboxViolation):
        scoped.read(
            request=request,
            spec=GET,
            envelope=envelope_for(view, scopes=READ_SCOPES),
            run_id=run.run_id,
            state_snapshot="s",
            tracer=tracer,
        )
    assert flaky_registry.reads == []
