"""The real gateway, approvals and ledger against a disposable Postgres, for the E-probes.

Throwaway. Nothing here is imported by a workflow module - activities only.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
from pathlib import Path

from agentstack.execution.gateway import ExecutionResult
from agentstack.interfaces.wiring import Stack, build_stack, envelope_for
from agentstack.observability.spans import Tracer
from agentstack.runtime.run import Run, new_run
from agentstack.storage import migrate
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import DEV_DATABASE_URL, open_pool
from agentstack.storage.provision import rebuild_database
from agentstack.tools.action import ActionRequest
from agentstack.tools.catalog import REFUND

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
TENANT = "acme"
SCOPES = frozenset({"billing:read", "billing:refund"})
_charges = itertools.count(1)


class Harness:
    def __init__(self) -> None:
        self._exit = contextlib.ExitStack()
        url = rebuild_database(DEV_DATABASE_URL, "agentstack_spike_enforcement_test")
        pool = self._exit.enter_context(open_pool(url, min_size=1, max_size=8))
        migrate.apply(pool, MIGRATIONS)
        cp_pool, saver = open_checkpointer(url)
        self._exit.callback(cp_pool.close)
        self.stack: Stack = build_stack(Database(pool=pool), saver, tenant=TENANT)
        # The world an approval is shown against: charge -> version.
        self.world: dict[str, int] = {}

    def close(self) -> None:
        self._exit.close()

    def new_run(self) -> Run:
        s = self.stack
        session = s.resolver.start(user_id="agent-operator", tenant=TENANT)
        return s.runs.ensure(
            new_run(
                session_id=session.session_id, tenant=TENANT, user="agent-operator", channel="spike"
            )
        )

    def new_charge(self) -> str:
        charge = f"ch-{next(_charges)}"
        self.world[charge] = 1
        return charge

    def snapshot(self, charge: str) -> str:
        return f"{charge}:v{self.world[charge]}"

    def refund(self, charge: str, key: str | None = None) -> ActionRequest:
        reg = self.stack.deps.registry
        req = reg.prepare(
            "issue_refund",
            {"tenant": TENANT, "customer_id": "c-42", "charge_id": charge, "amount_cents": 1999},
            exposed=reg.expose_for(tenant=TENANT),
        )
        return req if key is None else dataclasses.replace(req, idempotency_key=key)

    def grant(self, run_id: str, charge: str, snapshot: str, approver: str = "finance-oncall"):
        return self.stack.approvals.grant(
            run_id=run_id,
            request=self.refund(charge),
            state_snapshot=snapshot,
            approver=approver,
            summary=f"refund 19.99 on {charge}",
        )

    def execute(self, run_id: str, request: ActionRequest, snapshot: str) -> ExecutionResult:
        run = self.stack.runs.get(run_id)
        assert run is not None
        view = self.stack.resolver.resolve(
            session_id=run.session_id, user_id=run.user, tenant=run.tenant
        )
        return self.stack.deps.gateway.execute(
            request=request,
            spec=REFUND,
            envelope=envelope_for(view, scopes=SCOPES),
            run_id=run_id,
            state_snapshot=snapshot,
            tracer=Tracer(
                run_id=run_id, session_id=run.session_id, versions=self.stack.deps.versions
            ),
        )

    def commits_for(self, charge: str) -> int:
        return sum(1 for resource, _ in self.stack.client.calls if resource.endswith(f"/{charge}"))
