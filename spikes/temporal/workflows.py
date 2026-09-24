"""Workflows and activities the probes share (importable by the worker subprocess in P4)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

LEDGER = Path("/tmp/temporal-spike-ledger.jsonl")


def ledger_lines(tag: str) -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines() if line]
    return [r for r in rows if r["tag"] == tag]


def _append(row: dict) -> None:
    with LEDGER.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ---- P2: at-least-once --------------------------------------------------------
@activity.defn
async def charge(tag: str, key: str | None) -> str:
    """Commit the external effect, then 'die' before reporting completion on attempt 1."""
    if key is None or not any(r.get("key") == key for r in ledger_lines(tag)):
        _append({"tag": tag, "key": key, "attempt": activity.info().attempt})
    if activity.info().attempt == 1:
        raise ApplicationError("worker lost after the write landed")
    return "ok"


@workflow.defn
class ChargeWorkflow:
    @workflow.run
    async def run(self, tag: str, use_key: bool) -> str:
        key = f"{workflow.info().workflow_id}:charge" if use_key else None
        return await workflow.execute_activity(
            charge,
            args=[tag, key],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=100), maximum_attempts=3
            ),
        )


# ---- P1: identity ------------------------------------------------------------
@workflow.defn
class CycleWorkflow:
    @workflow.run
    async def run(self, hold: bool) -> str:
        if hold:
            await workflow.sleep(timedelta(seconds=3))
        return "done"


# ---- P3 / P4 / P8: approval wait ---------------------------------------------
@activity.defn
async def record(tag: str, what: str) -> None:
    _append({"tag": tag, "what": what})


@workflow.defn
class ApprovalWorkflow:
    """Prepare -> ask -> wait for an answer (re-ask on timeout) -> commit."""

    def __init__(self) -> None:
        self.answer: str | None = None
        self.wait_id = ""

    @workflow.run
    async def run(self, tag: str, reask_seconds: float, max_asks: int) -> str:
        opts = dict(start_to_close_timeout=timedelta(seconds=10))
        await workflow.execute_activity(record, args=[tag, "prepare"], **opts)
        for n in range(1, max_asks + 1):
            self.wait_id = f"wait-{n}"
            await workflow.execute_activity(record, args=[tag, f"ask:{self.wait_id}"], **opts)
            try:
                await workflow.wait_condition(
                    lambda: self.answer is not None, timeout=timedelta(seconds=reask_seconds)
                )
                break
            except TimeoutError:
                continue  # never silent expiry: ask again
        else:
            return "unanswered"
        await workflow.execute_activity(record, args=[tag, f"commit:{self.answer}"], **opts)
        return self.answer

    @workflow.update
    async def respond(self, wait_id: str, answer: str) -> str:
        del wait_id  # checked by the validator
        self.answer = answer
        return "accepted"

    @respond.validator
    def _check(self, wait_id: str, answer: str) -> None:
        del answer  # any answer is acceptable; only whether and which wait matters
        if self.answer is not None:
            raise ValueError("already answered")
        if wait_id != self.wait_id:
            raise ValueError(f"stale wait {wait_id!r}, pending is {self.wait_id!r}")

    @workflow.query
    def pending(self) -> str:
        return self.wait_id


# ---- P7: fan-out --------------------------------------------------------------
_inflight = 0
_peak = 0


@activity.defn
async def score(i: int) -> int:
    global _inflight, _peak
    _inflight += 1
    _peak = max(_peak, _inflight)
    await asyncio.sleep(0.3)
    _inflight -= 1
    return i


def peak() -> int:
    return _peak


@workflow.defn
class FanoutWorkflow:
    @workflow.run
    async def run(self, n: int) -> int:
        rs = await asyncio.gather(
            *[
                workflow.execute_activity(score, i, start_to_close_timeout=timedelta(seconds=10))
                for i in range(n)
            ]
        )
        return len(rs)
