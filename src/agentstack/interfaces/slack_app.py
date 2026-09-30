"""Slack's answers arriving over HTTP, through Bolt (ADR-0011).

The listener is a pipe: it hands the raw body and the two signature headers to
`wiring.answer` and holds no policy. `slack_callback.verify` is the authority on whether
the request is Slack's, fresh and first of its kind; Bolt's own signature check is a
second gate in front of it, never the only one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from slack_bolt import App
from temporalio.client import Client

from agentstack.interfaces.slack import answered_text, render_answered_blocks
from agentstack.interfaces.slack_callback import CallbackRefused, MalformedCallback
from agentstack.interfaces.wiring import Stack, answer
from agentstack.policy.approvers import ApproverNotAuthorized
from agentstack.runtime.approvals import ReplyNotApplicable, Resolution

log = logging.getLogger("agentstack.slack")

ACTIONS = ("approve_rollout", "refuse_rollout")
TIMESTAMP_HEADER = "x-slack-request-timestamp"
SIGNATURE_HEADER = "x-slack-signature"

# Each refusal is logged and acknowledged. Slack retries an interaction it did not see
# acknowledged, and a retry of a refused answer is refused again by the replay guard.
_REFUSALS = (CallbackRefused, MalformedCallback, ApproverNotAuthorized, ReplyNotApplicable)


def _header(headers: dict[str, Any], name: str) -> str:
    value = headers.get(name) or headers.get(name.title()) or ""
    return str(value[0]) if isinstance(value, (list, tuple)) else str(value)


def _show_answer(client: Any, body: dict[str, Any], resolution: Resolution) -> None:
    """Take the buttons off the message that was answered.

    Cosmetic: the answer is already recorded, so a failure here is logged and never
    raised - a 5xx would make Slack retry a click the replay guard will only refuse.
    `chat_update` rather than the click's `response_url`, which expires after thirty
    minutes and an approval can wait longer than that. The payload only picks which
    message to redraw; what it now says comes from the recorded answer.
    """
    message = body.get("message") or {}
    channel = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get(
        "channel_id"
    )
    if not channel or not message.get("ts"):
        log.warning("slack answer recorded, but the click names no message to update")
        return
    try:
        client.chat_update(
            channel=channel,
            ts=message["ts"],
            blocks=render_answered_blocks(
                list(message.get("blocks") or []),
                approved=resolution.approved,
                approver=resolution.approver,
            ),
            text=answered_text(approved=resolution.approved, approver=resolution.approver),
        )
    except Exception as exc:
        log.warning("slack answer recorded, but its message was not updated: %s", exc)


def build_app(
    stack: Stack,
    connect: Callable[[], Awaitable[Client]],
    *,
    signing_secret: str,
    token: str,
    **bolt: Any,
) -> App:
    # `token_verification_enabled` would call auth.test at construction; the worker's
    # notifier already fails loudly on a bad token, and tests have no workspace.
    app = App(
        token=token,
        signing_secret=signing_secret,
        token_verification_enabled=False,
        **bolt,
    )

    def on_answer(ack: Any, request: Any, body: dict[str, Any], client: Any) -> None:
        raw = request.raw_body
        raw_body = raw.encode() if isinstance(raw, str) else bytes(raw)
        try:

            async def go() -> Resolution:
                return await answer(
                    stack,
                    await connect(),
                    raw_body=raw_body,
                    sent_at=_header(request.headers, TIMESTAMP_HEADER),
                    signature=_header(request.headers, SIGNATURE_HEADER),
                    secret=signing_secret,
                )

            resolution = asyncio.run(go())
        except _REFUSALS as exc:
            log.warning("slack answer refused: %s: %s", type(exc).__name__, exc)
        else:
            _show_answer(client, body, resolution)
        ack()

    for action_id in ACTIONS:
        app.action(action_id)(on_answer)
    return app
