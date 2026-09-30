"""Run the Slack side of the workflow (ADR-0011).

    uv run agentstack-slack serve --port 3000
    uv run agentstack-slack approver add --slack-user U0123ABCD --principal you@acme
    uv run agentstack-slack trigger --experiment-id exp-7 --data-as-of <watermark>

`serve` needs SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET and refuses to start without
them: an approval nobody can answer is a run that waits forever.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from agentstack.interfaces.slack import TOKEN_VARIABLE
from agentstack.interfaces.slack_app import build_app
from agentstack.interfaces.slack_callback import SECRET_VARIABLE
from agentstack.interfaces.wiring import Stack, build_stack, deliver
from agentstack.runtime.temporal.client import connect, temporal_address
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import database_url, open_pool, redacted

TENANT = "acme"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="database URL; defaults to $DATABASE_URL")
    parser.add_argument("--address", default=None, help="Temporal; defaults to $TEMPORAL_ADDRESS")
    parser.add_argument("--tenant", default=TENANT)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="receive Slack's button clicks")
    serve.add_argument("--port", type=int, default=3000)
    approver = commands.add_parser("approver", help="who may answer for the tenant")
    approver.add_argument("action", choices=["add", "list"])
    approver.add_argument("--slack-user")
    approver.add_argument("--principal")
    trigger = commands.add_parser("trigger", help="fire a data_arrival trigger")
    trigger.add_argument("--experiment-id", required=True)
    trigger.add_argument("--data-as-of", required=True)
    args = parser.parse_args(argv)

    url = args.url or database_url()
    address = args.address or temporal_address()
    print(f"database : {redacted(url)}")
    checkpoints, saver = open_checkpointer(url)
    try:
        with open_pool(url, min_size=1, max_size=4) as pool:
            stack = build_stack(Database(pool=pool), saver, tenant=args.tenant)
            if args.command == "approver":
                return _approver(stack, args)
            if args.command == "trigger":
                payload = {
                    "kind": "data_arrival",
                    "experiment_id": args.experiment_id,
                    "data_as_of": args.data_as_of,
                    "tenant": args.tenant,
                }
                run_id = asyncio.run(_deliver(stack, address, payload))
                print(f"delivered : run {run_id}")
                return 0
            return _serve(stack, address, args.port)
    finally:
        checkpoints.close()


def _approver(stack: Stack, args: argparse.Namespace) -> int:
    if args.action == "list":
        for a in stack.approver_directory.for_tenant(args.tenant):
            print(f"{a.slack_user_id}  {a.principal}")
        return 0
    if not args.slack_user or not args.principal:
        print("FAIL  --slack-user and --principal are required", file=sys.stderr)
        return 2
    added = stack.approver_directory.add(
        tenant=args.tenant,
        slack_user_id=args.slack_user,
        principal=args.principal,
        added_by="agentstack-slack",
    )
    print(f"ok    {added.slack_user_id} may approve for {added.tenant}")
    return 0


async def _deliver(stack: Stack, address: str, payload: dict[str, Any]) -> str:
    return await deliver(stack, await connect(address), payload, source="slack-cli")


def _serve(stack: Stack, address: str, port: int) -> int:
    token = os.environ.get(TOKEN_VARIABLE, "").strip()
    secret = os.environ.get(SECRET_VARIABLE, "").strip()
    if not token or not secret:
        print(f"FAIL  {TOKEN_VARIABLE} and {SECRET_VARIABLE} must both be set", file=sys.stderr)
        return 1
    app = build_app(stack, lambda: connect(address), signing_secret=secret, token=token)
    print(f"listening: :{port}/slack/events  (set this as the Interactivity Request URL)")
    app.start(port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
