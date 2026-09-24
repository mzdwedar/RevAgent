"""E4: can the worker refuse an effect that does not go through the gateway?"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

from _env import local_env
from _eworker import running
from e_workflows import Rogue
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import ActivityInboundInterceptor, Interceptor, Worker

# Every activity a worker may run, and what it is. Undeclared means refused.
DECLARED = {"commit_refund": "gateway", "grant_approval": "policy", "snapshot_of": "read"}


class _Inbound(ActivityInboundInterceptor):
    async def execute_activity(self, input):
        name = activity.info().activity_type
        if name not in DECLARED:
            raise ApplicationError(
                f"{name} is not declared as a gateway, policy or read activity",
                type="UndeclaredActivity",
                non_retryable=True,
            )
        return await super().execute_activity(input)


class GatewayOnly(Interceptor):
    def intercept_activity(self, next):
        return _Inbound(next)


async def rogue(interceptors):
    async with running("e4", interceptors) as (env, h):
        ch = h.new_charge()
        out = await env.client.execute_workflow(
            Rogue.run, ch, id=f"e4-{uuid.uuid4()}", task_queue="e4"
        )
        return out, h.commits_for(ch)


async def leaky_import():
    from e4_leaky_wf import Leaky

    async with await local_env() as env:
        try:
            with ThreadPoolExecutor(1) as pool:
                async with Worker(
                    env.client, task_queue="e4b", workflows=[Leaky], activity_executor=pool
                ):
                    return await env.client.execute_workflow(
                        Leaky.run, id=f"e4b-{uuid.uuid4()}", task_queue="e4b"
                    )
        except RuntimeError as e:
            return f"refused at worker start: {e}"


async def main():
    bare = await rogue(())
    guarded = await rogue([GatewayOnly()])
    print(f"  no interceptor:        {bare}")
    print(f"  GatewayOnly allowlist: {guarded}")
    assert bare[0].startswith("receipt") and bare[1] == 1, bare
    assert guarded == ("refused:UndeclaredActivity", 0), guarded
    leaky = await leaky_import()
    print(f"  workflow module importing the gateway: {leaky!r}")
    assert leaky == "ran", leaky  # the sandbox guards determinism, not layer boundaries
    print(
        "E4 PASS: an allowlist interceptor refuses undeclared activities; the sandbox "
        "lets a workflow import the gateway, so only an import contract can forbid it"
    )


asyncio.run(main())
