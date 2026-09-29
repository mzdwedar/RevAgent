"""Probe workflows for `test_temporal_boundaries`.

A module of their own because the workflow sandbox re-imports whatever defines a
workflow, and a test module drags pytest and the gateway in with it.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from agentstack.runtime.temporal.retry import RETRY


@workflow.defn
class CallOnce:
    """Runs one named activity under the production policy and reports how it ended."""

    @workflow.run
    async def run(self, activity_name: str, arg: str) -> str:
        try:
            await workflow.execute_activity(
                activity_name,
                arg,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RETRY,
            )
        except ActivityError as exc:
            cause = exc.cause
            if isinstance(cause, ApplicationError):
                return f"{cause.type}:non_retryable={cause.non_retryable}"
            return type(cause).__name__
        return "completed"
