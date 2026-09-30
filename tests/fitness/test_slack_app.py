"""ADR-0011: Bolt is an adapter. Only the interfaces layer imports it, and its listener
is a pipe into `wiring.answer` that holds no policy of its own."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from temporalio.client import Client

from agentstack.interfaces import slack_app
from agentstack.interfaces.wiring import Stack
from agentstack.runtime.approvals import ReplyNotApplicable, Resolution

SRC = Path(__file__).resolve().parents[2] / "src" / "agentstack"


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_only_the_interfaces_layer_imports_bolt() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "slack_bolt" in _imports(p) and p.parent.name != "interfaces"
    ]
    assert offenders == []


def test_the_listener_passes_raw_bytes_and_headers_to_answer() -> None:
    source = inspect.getsource(slack_app)
    assert "answer(" in source and "raw_body=raw_body" in source
    assert "signature=" in source and "sent_at=" in source
    # No decision of its own: policy and parsing stay in layers 8 and 1's callback module.
    for forbidden in ("parse_interaction", "authorize_approver", "directory"):
        assert forbidden not in source


_RESOLUTION = Resolution(
    run_id="r", wait_id="w", approved=True, approver="ana@acme", approval_id="a-1"
)


def _click(*, signed_with: str) -> tuple[bytes, dict[str, str]]:
    import json
    import time
    from urllib.parse import urlencode

    from agentstack.interfaces.slack_callback import expected_signature

    payload = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "team": {"id": "T1"},
        "actions": [{"action_id": "approve_rollout", "value": "r|w|e|d", "type": "button"}],
        "message": {
            "ts": "1790.01",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "Roll out to ~10"}},
                {"type": "actions", "elements": [{"type": "button", "action_id": "approve"}]},
            ],
        },
    }
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    stamp = str(int(time.time()))
    headers = {
        "x-slack-request-timestamp": stamp,
        "x-slack-signature": expected_signature(signed_with, sent_at=stamp, raw_body=raw),
        "content-type": "application/x-www-form-urlencoded",
    }
    return raw, headers


def test_a_signed_click_reaches_answer_and_a_forged_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slack_bolt.authorization import AuthorizeResult
    from slack_bolt.request import BoltRequest

    calls: list[dict[str, object]] = []

    async def fake_answer(_stack: object, _client: object, **kwargs: object) -> Resolution:
        calls.append(kwargs)
        return _RESOLUTION

    async def fake_connect() -> object:
        return object()

    monkeypatch.setattr(slack_app, "answer", fake_answer)
    app = slack_app.build_app(
        cast(Stack, object()),
        cast(Callable[[], Awaitable[Client]], fake_connect),
        signing_secret="s3cret",
        token="xoxb-test",
        # Bolt would otherwise call auth.test on Slack for every request.
        authorize=lambda **_: AuthorizeResult(
            enterprise_id=None, team_id="T1", bot_token="xoxb-test"
        ),
    )

    raw, headers = _click(signed_with="s3cret")
    ok = app.dispatch(BoltRequest(body=raw.decode(), headers=headers))
    assert ok.status == 200
    assert len(calls) == 1
    assert calls[0]["raw_body"] == raw
    assert calls[0]["signature"] == headers["x-slack-signature"]
    assert calls[0]["sent_at"] == headers["x-slack-request-timestamp"]

    raw, headers = _click(signed_with="wrong")
    bad = app.dispatch(BoltRequest(body=raw.decode(), headers=headers))
    assert bad.status == 401
    assert len(calls) == 1, "a forged click never reached answer"


def _dispatch_click(
    monkeypatch: pytest.MonkeyPatch,
    answer_fn: object,
    *,
    update_raises: bool = False,
) -> tuple[int, list[dict]]:
    """One signed click through Bolt with `answer` faked; returns the status and every
    `chat_update` Slack would have been sent."""
    from slack_bolt.authorization import AuthorizeResult
    from slack_bolt.request import BoltRequest
    from slack_sdk import WebClient

    updates: list[dict] = []

    def chat_update(_self: object, **kwargs: object) -> None:
        updates.append(kwargs)
        if update_raises:
            raise RuntimeError("slack is down")

    async def fake_connect() -> object:
        return object()

    monkeypatch.setattr(slack_app, "answer", answer_fn)
    monkeypatch.setattr(WebClient, "chat_update", chat_update)
    app = slack_app.build_app(
        cast(Stack, object()),
        cast(Callable[[], Awaitable[Client]], fake_connect),
        signing_secret="s3cret",
        token="xoxb-test",
        authorize=lambda **_: AuthorizeResult(
            enterprise_id=None, team_id="T1", bot_token="xoxb-test"
        ),
    )
    raw, headers = _click(signed_with="s3cret")
    response = app.dispatch(BoltRequest(body=raw.decode(), headers=headers))
    return response.status, updates


async def _answered(*_a: object, **_k: object) -> Resolution:
    return _RESOLUTION


async def _refused(*_a: object, **_k: object) -> Resolution:
    raise ReplyNotApplicable("a question nobody asked")


def test_an_answered_message_loses_its_buttons_and_names_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, updates = _dispatch_click(monkeypatch, _answered)

    assert status == 200
    (update,) = updates
    assert (update["channel"], update["ts"]) == ("C1", "1790.01")
    assert [b["type"] for b in update["blocks"]] == ["header", "section"]
    assert "Approved by ana@acme" in update["blocks"][-1]["text"]["text"]
    assert update["text"] == "Approved by ana@acme"


def test_a_refused_click_leaves_the_message_as_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    status, updates = _dispatch_click(monkeypatch, _refused)

    assert status == 200
    assert updates == [], "nothing was answered, so nothing is redrawn"


def test_a_failed_update_does_not_fail_an_answer_that_is_already_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, updates = _dispatch_click(monkeypatch, _answered, update_raises=True)

    assert len(updates) == 1, "the update was attempted"
    assert status == 200, "and its failure is not Slack's to retry"
