"""The operator's view of runs that are waiting (layer 1, criterion 4).

    uv run agentstack-operator stalled --older-than 7d

`stalled` exists because trigger-based waiting fails silently: a run whose trigger
never came looks exactly like one waiting patiently, unless something asks. This is
the thing that asks.

Exits 1 when anything is stalled, so a scheduler running it can alert on the exit code
rather than on someone reading the output.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta

from agentstack.runtime.run import RunStore
from agentstack.runtime.waits import WaitStore
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
    stalled = commands.add_parser("stalled", help="trigger waits past their deadline")
    stalled.add_argument(
        "--older-than",
        type=duration,
        default=timedelta(0),
        help="only waits parked at least this long ago (the deadline decides what is stalled)",
    )
    parser.add_argument("--url", default=None, help="database URL; defaults to $DATABASE_URL")
    args = parser.parse_args(argv)

    url = args.url or database_url()
    now = now or datetime.now(UTC)
    print(f"database : {redacted(url)}")

    with open_pool(url, min_size=1, max_size=2) as pool:
        db = Database(pool=pool)
        waits = WaitStore(db=db).stalled(now=now, older_than=args.older_than)
        runs = RunStore(db=db)
        for wait in waits:
            assert wait.deadline is not None  # the query only returns waits that have one
            run = runs.get(wait.run_id)
            assert run is not None  # waits.run_id is a foreign key onto runs
            print(
                f"  stalled  {wait.wait_id}  run {wait.run_id}  tenant {run.tenant}  "
                f"due {wait.deadline:%Y-%m-%d %H:%M%z}  overdue {_ago(now - wait.deadline)}"
            )

    print(f"{len(waits)} stalled")
    return 1 if waits else 0


def _ago(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return f"{hours // 24}d{hours % 24}h" if hours >= 24 else f"{hours}h"


if __name__ == "__main__":
    sys.exit(main())
