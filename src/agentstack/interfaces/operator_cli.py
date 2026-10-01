"""The operator's view of runs that are waiting (layer 1, criterion 4).

    uv run agentstack-operator stalled --older-than 7d

`stalled` exists because trigger-based waiting fails silently: a run whose trigger
never came looks exactly like one waiting patiently, unless something asks. This is
the thing that asks. It also lists runs parked in `needs_migration` (criterion 21),
labelled separately: nothing is late there, the code moved under them. And evaluation
cycles claimed long ago that never settled: layer 8 refused the outcome, or the cycle
is stuck retrying. A refusal left unsettled on purpose is only useful if it's seen.

Exits 1 when anything is stalled, so a scheduler running it can alert on the exit code
rather than on someone reading the output.

Runs parked on a `reconcile` wait are listed too, on their own line, from the moment
they park: an effect of unknown outcome (possibly a rollout customers can already see)
is settled by nobody but a person, and the run, and every trigger queued behind it,
waits until then. `reconcile` is how that person settles it:

    uv run agentstack-operator reconcile WAIT_ID --applied RECEIPT \\
        --operator ana@acme --reason "registry shows the rollout at 10%"
    uv run agentstack-operator reconcile WAIT_ID --not-applied \\
        --operator ana@acme --reason "no rollout event in the registry"

It settles the claim, audits who did and why, satisfies the wait, and wakes the run.
Running it again finishes a settlement that died halfway, and changes nothing otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from agentstack.execution.idempotency import IdempotencyLedger
from agentstack.observability.audit import AuditSink
from agentstack.runtime.cycles import UNSETTLED_AFTER, CycleStore
from agentstack.runtime.reconcile import ReconcileRefused, settle
from agentstack.runtime.run import RunStore
from agentstack.runtime.temporal.client import (
    TemporalUnavailable,
    connect,
    notify_answer,
    position,
    temporal_address,
)
from agentstack.runtime.waits import NEEDS_MIGRATION, RECONCILE, WaitStore
from agentstack.storage.database import Database
from agentstack.storage.pool import database_url, open_pool, redacted

_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def duration(text: str) -> timedelta:
    """`90s`, `15m`, `6h`, `7d`. A bare number is refused: minutes or days is not a guess."""
    match = re.fullmatch(r"(\d+)([smhd])", text.strip())
    if match is None:
        raise argparse.ArgumentTypeError(f"{text!r} is not a duration like 30m, 6h or 7d")
    amount, unit = match.groups()
    return timedelta(**{_UNITS[unit]: int(amount)})


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report runs that are waiting too long.")
    commands = parser.add_subparsers(dest="command", required=True)
    stalled = commands.add_parser(
        "stalled", help="trigger waits past their deadline, and runs in needs_migration"
    )
    stalled.add_argument(
        "--older-than",
        type=duration,
        default=timedelta(0),
        help="only waits parked at least this long ago (the deadline decides what is stalled)",
    )
    status = commands.add_parser(
        "status", help="one run: its record (Postgres) beside its position (Temporal)"
    )
    status.add_argument("run_id")
    status.add_argument("--address", default=None, help="Temporal; defaults to $TEMPORAL_ADDRESS")
    reconcile = commands.add_parser(
        "reconcile",
        help="settle an effect of unknown outcome, after checking the surface, and wake its run",
    )
    reconcile.add_argument("wait_id", help="the reconcile wait `stalled` lists")
    verdict = reconcile.add_mutually_exclusive_group(required=True)
    verdict.add_argument(
        "--applied", metavar="RECEIPT", help="the surface did apply it; its receipt for the effect"
    )
    verdict.add_argument(
        "--not-applied", action="store_true", help="the surface shows it did not apply"
    )
    reconcile.add_argument("--operator", required=True, help="who checked the surface")
    reconcile.add_argument("--reason", required=True, help="what the surface was found to show")
    reconcile.add_argument(
        "--address", default=None, help="Temporal; defaults to $TEMPORAL_ADDRESS"
    )
    parser.add_argument("--url", default=None, help="database URL; defaults to $DATABASE_URL")
    args = parser.parse_args(argv)

    url = args.url or database_url()
    now = now or datetime.now(UTC)
    print(f"database : {redacted(url)}")

    if args.command == "status":
        with open_pool(url, min_size=1, max_size=2) as pool:
            return asyncio.run(
                _status(Database(pool=pool), args.run_id, args.address or temporal_address())
            )
    if args.command == "reconcile":
        address = args.address or temporal_address()
        with open_pool(url, min_size=1, max_size=2) as pool:
            return asyncio.run(
                reconcile_report(
                    lambda: connect(address),
                    Database(pool=pool),
                    wait_id=args.wait_id,
                    receipt=args.applied,
                    operator=args.operator,
                    reason=args.reason,
                )
            )

    with open_pool(url, min_size=1, max_size=2) as pool:
        db = Database(pool=pool)
        waits = WaitStore(db=db).stalled(now=now, older_than=args.older_than)
        runs = RunStore(db=db)
        for wait in waits:
            run = runs.get(wait.run_id)
            assert run is not None  # waits.run_id is a foreign key onto runs
            where = f"{wait.wait_id}  run {wait.run_id}  tenant {run.tenant}"
            if wait.kind == NEEDS_MIGRATION:
                print(
                    f"  {NEEDS_MIGRATION}  {where}  parked {_ago(now - wait.created_at)} ago  "
                    f"checkpoint {wait.state_snapshot}"
                )
                continue
            if wait.kind == RECONCILE:
                assert wait.deadline is not None  # pending_reconcile_waits_have_a_deadline
                due = (
                    f"OVERDUE {_ago(now - wait.deadline)}"
                    if wait.deadline <= now
                    else f"due {wait.deadline:%Y-%m-%d %H:%M%z}"
                )
                print(
                    f"  {RECONCILE}  {where}  parked {_ago(now - wait.created_at)} ago  "
                    f"{due}  claim {wait.idempotency_key}  "
                    f"settle: agentstack-operator reconcile {wait.wait_id}"
                )
                continue
            assert wait.deadline is not None  # a stalled trigger wait is one past its deadline
            print(
                f"  stalled  {where}  due {wait.deadline:%Y-%m-%d %H:%M%z}  "
                f"overdue {_ago(now - wait.deadline)}"
            )
        # Every cycle is unsettled while it scores; only one that stays that way is news.
        claimed_before = now - max(args.older_than, UNSETTLED_AFTER)
        cycles = [c for c in CycleStore(db=db).unsettled() if c.claimed_at <= claimed_before]
        for cycle in cycles:
            print(
                f"  unsettled  {cycle.experiment_id} @ {cycle.data_as_of}  "
                f"kind {cycle.kind.value}  claimed {_ago(now - cycle.claimed_at)} ago"
            )

    migrations = sum(1 for w in waits if w.kind == NEEDS_MIGRATION)
    reconciling = sum(1 for w in waits if w.kind == RECONCILE)
    print(
        f"{len(waits) - migrations - reconciling} stalled, {migrations} {NEEDS_MIGRATION}, "
        f"{reconciling} {RECONCILE}, {len(cycles)} unsettled"
    )
    return 1 if waits or cycles else 0


async def reconcile_report(
    connect_to: Callable[[], Awaitable[Client]],
    db: Database,
    *,
    wait_id: str,
    receipt: str | None,
    operator: str,
    reason: str,
) -> int:
    """Settle the claim in the record, then wake the run. `receipt` None: not applied.

    The record comes first and is complete on its own (layer 3 decides, in `settle`): a
    run whose wake-up is lost is woken by running this again, which changes nothing else.
    Exits 1 when the settlement is refused or the run could not be woken.
    """
    try:
        settled = settle(
            runs=RunStore(db=db),
            waits=WaitStore(db=db),
            ledger=IdempotencyLedger(db=db),
            audit=AuditSink(db=db),
            wait_id=wait_id,
            applied=receipt is not None,
            receipt=receipt,
            operator=operator,
            reason=reason,
        )
    except ReconcileRefused as exc:
        print(f"refused  {exc}")
        return 1
    verdict = "applied" if settled.applied else "not applied"
    news = "settled" if settled.changed else "already settled"
    print(f"record   {settled.wait.wait_id}  {news} as {verdict}  run {settled.run.run_id}")
    try:
        await notify_answer(
            await connect_to(), run_id=settled.run.run_id, wait_id=settled.wait.wait_id
        )
    except TemporalUnavailable as exc:
        print(f"position not woken: {exc}. Run this command again to wake it.")
        return 1
    except RPCError as exc:
        if exc.status is not RPCStatusCode.NOT_FOUND:
            raise
        print("position no workflow for this run; its next turn finds the wait satisfied")
        return 0
    print("position woken; the run acts again and the ledger answers")
    return 0


async def _status(db: Database, run_id: str, address: str) -> int:
    """The record first: a run Postgres doesn't know is answered without asking Temporal."""
    if RunStore(db=db).get(run_id) is None:
        print(f"{run_id} is not a run")
        return 1
    return await status_report(await connect(address), db, run_id)


async def status_report(client: Client, db: Database, run_id: str) -> int:
    """Print what the record says beside where Temporal says the run is.

    Position is Temporal's: whether the workflow runs, what it is waiting on, which
    activity is pending and how many times it has been tried. Everything the run did
    (its waits, and what it asked and committed) is the record's, in Postgres. Neither
    is taken for the other. Exits 1 when the run is unknown or an activity is stuck.
    """
    run = RunStore(db=db).get(run_id)
    if run is None:
        print(f"{run_id} is not a run")
        return 1
    print(
        f"run      {run.run_id}  tenant {run.tenant}  stage {run.stage}  session {run.session_id}"
    )
    pending = WaitStore(db=db).pending_for(run_id)
    for wait in pending:
        due = "no deadline" if wait.deadline is None else f"due {wait.deadline:%Y-%m-%d %H:%M%z}"
        print(f"record   waiting  {wait.kind}  {wait.wait_id}  {due}  asked again {wait.reasks}x")
    if not pending:
        print("record   no pending wait")

    where = await position(client, run_id)
    if where is None:
        print("position no workflow for this run")
        return 0
    print(f"position workflow {where.status}")
    progress = where.progress
    if progress is None and where.status == "RUNNING":
        print("position no worker answered; the run is not advancing until one polls")
    elif progress is not None:
        if progress.awaiting_approval:
            print(f"position awaiting approval {progress.awaiting_approval}, put {progress.asks}x")
        if progress.reconciling:
            print(f"position awaiting reconciliation {progress.reconciling}")
        if progress.waiting_on:
            late = "  overdue" if progress.overdue else ""
            print(f"position waiting on trigger wait {progress.waiting_on}{late}")
        for act in progress.commits:
            if act.status == "refused":
                print(f"position act refused {act.refusal}")
        print(
            f"position {progress.cycles_before + len(progress.cycles)} cycles, "
            f"{len(progress.commits)} acts, {progress.pending_triggers} triggers queued"
        )
    stuck = where.stuck()
    for activity in where.pending:
        flag = "  STUCK" if activity in stuck else ""
        print(f"activity {activity.name}  attempt {activity.attempt}{flag}")
        if flag:
            print(f"         last failure: {activity.last_failure or 'none recorded'}")
    return 1 if stuck else 0


def _ago(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return f"{hours // 24}d{hours % 24}h" if hours >= 24 else f"{hours}h"


if __name__ == "__main__":
    sys.exit(main())
