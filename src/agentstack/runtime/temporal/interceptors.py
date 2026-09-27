"""Only declared activities run (ADR-0008 rule 5, E4).

An activity is an execution unit, not an authority. Being registered on a worker is not
a declaration: anything importable can be registered, and E4 showed an activity
registered next to the real ones reaching a surface client around the gateway. So
each activity says here what it is, and the worker refuses anything else before its
body runs, non-retryably.

The kinds are for the reader. The one that matters is `gateway`: an activity declared
as `gateway` commits only through `gateway.execute`, and it's the only kind that
commits at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from agentstack.runtime.temporal.contracts import (
    ENSURE_RUN,
    EVALUATE_CYCLE,
    PARK_TRIGGER_WAIT,
    RUN_TURN,
    SATISFY_TRIGGER_WAIT,
)
from agentstack.runtime.temporal.retry import UNDECLARED

DECLARED: Mapping[str, str] = {
    ENSURE_RUN: "record",
    # Scores and claims a cycle. Writes only `trigger_cycles`; acts on nothing.
    EVALUATE_CYCLE: "record",
    PARK_TRIGGER_WAIT: "record",
    SATISFY_TRIGGER_WAIT: "record",
    # The turn: the only activity so far that commits, and only through gateway.execute.
    RUN_TURN: "gateway",
}


class DeclaredActivitiesOnly(Interceptor):
    def __init__(self, declared: Mapping[str, str] = DECLARED) -> None:
        self._declared = declared

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _Inbound(next, self._declared)


class _Inbound(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, declared: Mapping[str, str]) -> None:
        super().__init__(next)
        self._declared = declared

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        name = activity.info().activity_type
        if name not in self._declared:
            raise ApplicationError(
                f"{name} is not a declared activity; declare it in "
                "runtime/temporal/interceptors.py with what it is before a worker may run it",
                type=UNDECLARED,
                non_retryable=True,
            )
        return await super().execute_activity(input)
