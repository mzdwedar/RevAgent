"""The operator's channel onto the schema (layer 1).

    uv run agentstack-migrate status
    uv run agentstack-migrate up
    uv run agentstack-migrate down --to 3

Kept out of `agentstack.interfaces.cli`, which is the stack walkthrough: migrating a
database and demonstrating a refund are not two modes of one command.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentstack.storage import migrate
from agentstack.storage.pool import database_url, open_pool, redacted

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or roll back schema migrations.")
    parser.add_argument("action", choices=("up", "down", "status"))
    parser.add_argument("--to", type=int, help="target version for `down` (0 unwinds everything)")
    parser.add_argument("--url", default=None, help="database URL; defaults to $DATABASE_URL")
    parser.add_argument("--directory", type=Path, default=MIGRATIONS)
    args = parser.parse_args(argv)

    if args.action == "down" and args.to is None:
        parser.error("down needs --to; rolling back an unstated number of steps is not a plan")

    url = args.url or database_url()
    print(f"database  : {redacted(url)}")
    print(f"migrations: {args.directory}")

    with open_pool(url, min_size=1, max_size=2) as pool:
        if args.action == "status":
            for record in migrate.applied(pool):
                print(f"  applied  {record.label}  {record.applied_at:%Y-%m-%d %H:%M:%S%z}")
            outstanding = migrate.pending(pool, args.directory)
            for migration in outstanding:
                print(f"  pending  {migration.label}")
            print(f"{len(outstanding)} pending")
            return 0

        if args.action == "up":
            changed = migrate.apply(pool, args.directory)
            verb = "applied"
        else:
            changed = migrate.rollback_to(pool, args.directory, version=args.to)
            verb = "rolled back"

    for migration in changed:
        print(f"  {verb} {migration.label}")
    print(f"{len(changed)} {verb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
