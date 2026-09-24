"""Trigger fan-out without a stampede (T21).

One data batch lands and every experiment watching it fires at once. Evaluating them all
at once is the stampede: an evaluation scores a cohort, and scoring is the expensive
part of the system - seconds of CPU and a model's worth of memory each. Nothing about a
trigger arriving means its evaluation has to start that instant.

So the batch is drained through a fixed number of workers. Two things are *not* this
module's job, because they already hold under contention and T21 verifies that rather
than re-implementing it:

* **Duplicates.** A redelivered trigger in the same batch is settled by the cycle claim
  in `cycles.evaluate` - one statement, so two workers racing it produce one evaluation.
* **Connections.** An evaluation holds at most one pooled connection at a time, so any
  bound at or below the pool size never queues on the pool.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from agentstack.policy.triggers import TriggerEvent
from agentstack.runtime.cycles import Cycle, CycleStore, Evaluator, evaluate
from agentstack.storage.pool import DEFAULT_MAX_SIZE

# Below the app pool (so no evaluation waits for a connection) and a small multiple of
# a laptop's cores (so scoring does not oversubscribe the CPU). A number to revisit with
# real TabPFN scoring under load; the bound itself is the design.
DEFAULT_MAX_IN_FLIGHT = min(8, DEFAULT_MAX_SIZE)


@dataclass(frozen=True, slots=True)
class Delivery:
    """What became of one trigger. Exactly one of `cycle` and `error` is set."""

    trigger: TriggerEvent
    cycle: Cycle | None
    error: BaseException | None


def fan_out(
    store: CycleStore,
    triggers: Sequence[TriggerEvent],
    evaluator: Evaluator,
    *,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
) -> tuple[Delivery, ...]:
    """Evaluate a batch of triggers, at most `max_in_flight` at a time.

    One failed evaluation does not stop the rest, and is returned rather than raised:
    the batch is many independent experiments, and one bad cohort must not leave the
    others unevaluated. A failed cycle stays claimed and unsettled, which is where
    `CycleStore.unsettled` finds it.
    """
    if max_in_flight < 1:
        raise ValueError(f"max_in_flight={max_in_flight} would evaluate nothing")
    with ThreadPoolExecutor(max_workers=max_in_flight, thread_name_prefix="trigger") as workers:
        futures = [workers.submit(evaluate, store, trigger, evaluator) for trigger in triggers]
    deliveries: list[Delivery] = []
    for trigger, future in zip(triggers, futures, strict=True):
        error = future.exception()
        deliveries.append(
            Delivery(trigger=trigger, cycle=None if error else future.result(), error=error)
        )
    return tuple(deliveries)
