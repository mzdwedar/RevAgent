"""Workflows for the E-probes. No agentstack import: activities are named, not imported."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError


def _retry(non_retryable: list[str] | None = None, attempts: int = 3) -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(milliseconds=100),
        backoff_coefficient=1.0,
        maximum_attempts=attempts,
        non_retryable_error_types=non_retryable,
    )


def _cause_type(e: ActivityError) -> str:
    c = e.cause
    return c.type if isinstance(c, ApplicationError) and c.type else type(c).__name__


T = timedelta(seconds=30)


@workflow.defn
class KeyDerivation:
    @workflow.run
    async def run(self, run_id: str, charge: str, key_mode: str, lose_first: bool) -> str:
        return await workflow.execute_activity(
            "commit_refund",
            args=[run_id, charge, key_mode, "", lose_first],
            start_to_close_timeout=T,
            retry_policy=_retry(),
        )


@workflow.defn
class Unresolved:
    def __init__(self) -> None:
        self.parked = False
        self.reconciled = False

    @workflow.run
    async def run(self, run_id: str, charge: str, non_retryable: bool) -> str:
        policy = _retry(["UnresolvedEffect"] if non_retryable else None, attempts=5)
        args = [run_id, charge, "content", "", False]
        try:
            return await workflow.execute_activity(
                "commit_refund", args=args, start_to_close_timeout=T, retry_policy=policy
            )
        except ActivityError as e:
            if not non_retryable:
                return f"failed:{_cause_type(e)}"
        # Park until someone has asked the surface what really happened.
        self.parked = True
        await workflow.wait_condition(lambda: self.reconciled)
        return await workflow.execute_activity(
            "commit_refund", args=args, start_to_close_timeout=T, retry_policy=policy
        )

    @workflow.signal
    def reconcile_done(self) -> None:
        self.reconciled = True

    @workflow.query
    def is_parked(self) -> bool:
        return self.parked


@workflow.defn
class ApproveThenAct:
    def __init__(self) -> None:
        self.answer: str | None = None
        self.go = False

    @workflow.run
    async def run(self, run_id: str, charge: str, snapshot_mode: str) -> str:
        snap = await workflow.execute_activity("snapshot_of", charge, start_to_close_timeout=T)
        await workflow.wait_condition(lambda: self.answer is not None)
        try:
            await workflow.execute_activity(
                "grant_approval",
                args=[run_id, charge, snap, self.answer],
                start_to_close_timeout=T,
                retry_policy=_retry(["ApproverNotAuthorized"]),
            )
        except ActivityError as e:
            return f"refused:{_cause_type(e)}"
        await workflow.wait_condition(lambda: self.go)  # the gap in which the world moves
        passed = snap if snapshot_mode == "workflow" else ""
        try:
            return await workflow.execute_activity(
                "commit_refund",
                args=[run_id, charge, "content", passed, False],
                start_to_close_timeout=T,
                retry_policy=_retry(["ApprovalStale", "ApprovalRequired", "PolicyDenied"]),
            )
        except ActivityError as e:
            return f"refused:{_cause_type(e)}"

    @workflow.update
    def respond(self, claimed_user_id: str) -> str:
        self.answer = claimed_user_id
        return "accepted"

    @respond.validator
    def _shape_only(self, claimed_user_id: str) -> None:
        # All a validator can do: it runs in the deterministic sandbox, with no I/O,
        # so it cannot consult the approver directory.
        if self.answer is not None:
            raise ValueError("already answered")
        if not claimed_user_id.strip():
            raise ValueError("an answer must name who gave it")

    @workflow.signal
    def proceed(self) -> None:
        self.go = True


@workflow.defn
class Rogue:
    @workflow.run
    async def run(self, charge: str) -> str:
        try:
            return await workflow.execute_activity(
                "rogue_refund", charge, start_to_close_timeout=T, retry_policy=_retry()
            )
        except ActivityError as e:
            return f"refused:{_cause_type(e)}"
