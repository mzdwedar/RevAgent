"""Cancelling a run while its turn is in flight (audit M5).

The turn is one blocking activity with a heartbeat thread beside it. Cancellation had
never been exercised: nothing showed that a cancelled run's turn stops rather than
finishing in the background and committing a draft nobody is waiting for any more. The
heartbeat is also the only way the worker hears of the cancel, so the beat has to keep
going, and has to stop with the turn.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import CancelledError

from agentstack.interfaces.wiring import Stack, build_stack, deliver
from agentstack.storage.database import Database
from tests.durability.test_turn_activity import PAYLOAD, handle_of, worker
from tests.temporal_support import DRAFT_TOOL, DraftingEngine

pytestmark = pytest.mark.usefixtures("fixture_dataset")

# The worker hears of a cancel on a heartbeat, which is sent every few seconds.
HEARD_WITHIN_S = 20.0


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


class HeldDraftingEngine(DraftingEngine):
    """A model that answers the drafting turn only when the test lets it."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.returned_at: float | None = None

    def generate(self, request: Any) -> Any:
        if DRAFT_TOOL in request.tool_names:
            self.started.set()
            self.release.wait(timeout=60)
            self.returned_at = time.monotonic()
        return super().generate(request)


def beating() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "turn-heartbeat" and t.is_alive()]


def test_a_run_cancelled_mid_turn_commits_nothing_and_stops_beating(
    stack: Stack, app_database: Database, temporal_address: str, task_queue: str
) -> None:
    engine = HeldDraftingEngine()

    async def cancel_in_flight() -> str:
        async with worker(stack, app_database, temporal_address, task_queue, engine) as client:
            run_id = await deliver(stack, client, PAYLOAD, source="t", task_queue=task_queue)
            handle = handle_of(client, run_id)
            assert await asyncio.to_thread(engine.started.wait, 30), "the turn never started"
            assert beating(), "a turn in flight is beating"

            await handle.cancel()
            with pytest.raises(WorkflowFailureError) as ended:
                await asyncio.wait_for(handle.result(), timeout=30)
            assert isinstance(ended.value.cause, CancelledError)

            # The worker learns of the cancel on a heartbeat. Let it, and then let the
            # model answer: a turn that ignored the cancel would go on to draft.
            await asyncio.sleep(HEARD_WITHIN_S / 2)
            engine.release.set()
            deadline = time.monotonic() + HEARD_WITHIN_S
            while beating() and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            return run_id

    run_id = asyncio.run(asyncio.wait_for(cancel_in_flight(), timeout=90))

    assert stack.registry_client.drafts == {}, "a cancelled run's turn committed after the cancel"
    assert [r for r in stack.audit.for_run(run_id) if r.outcome == "committed"] == []
    assert beating() == [], "the heartbeat outlived the turn it was for"
