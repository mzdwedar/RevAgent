"""ADR-0011: `agentstack-slack` is the operator's way to say who may approve, to fire a
trigger and to start the receiver. The approver commands run against Postgres; the two
that reach the network (`trigger`, `serve`) are exercised at their outermost edge."""

from __future__ import annotations

from typing import Any

import pytest

from agentstack.interfaces import slack_cli
from agentstack.interfaces.slack import TOKEN_VARIABLE
from agentstack.interfaces.slack_callback import SECRET_VARIABLE


@pytest.fixture
def url(request: pytest.FixtureRequest, app_database_url: str) -> str:
    """The migrated database: the checkpointer the CLI opens needs its schema."""
    request.getfixturevalue("app_database")
    return app_database_url


def _run(url: str, *argv: str) -> int:
    return slack_cli.main(["--url", url, "--tenant", "acme-cli", *argv])


def test_an_approver_added_is_listed_for_that_tenant(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(url, "approver", "add", "--slack-user", "UCLI1", "--principal", "ana@acme") == 0
    assert "UCLI1 may approve for acme-cli" in capsys.readouterr().out

    assert _run(url, "approver", "list") == 0
    assert "UCLI1  ana@acme" in capsys.readouterr().out


def test_adding_an_approver_needs_both_the_slack_id_and_the_principal(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(url, "approver", "add", "--slack-user", "UCLI2") == 2
    assert "--slack-user and --principal are required" in capsys.readouterr().err

    assert _run(url, "approver", "list") == 0
    assert "UCLI2" not in capsys.readouterr().out


def test_trigger_delivers_a_data_arrival_for_the_named_experiment(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    async def deliver(_stack: Any, address: str, payload: dict[str, Any]) -> str:
        seen.update(address=address, payload=payload)
        return "run-1"

    monkeypatch.setattr(slack_cli, "_deliver", deliver)

    code = _run(
        url,
        "--address",
        "temporal.test:7233",
        "trigger",
        "--experiment-id",
        "exp-7",
        "--data-as-of",
        "telecom:abc",
    )

    assert code == 0
    assert "delivered : run run-1" in capsys.readouterr().out
    assert seen["address"] == "temporal.test:7233"
    assert seen["payload"] == {
        "kind": "data_arrival",
        "experiment_id": "exp-7",
        "data_as_of": "telecom:abc",
        "tenant": "acme-cli",
    }


@pytest.mark.parametrize("missing", [TOKEN_VARIABLE, SECRET_VARIABLE])
def test_serve_refuses_to_start_without_both_credentials(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    """An approval nobody can answer is a run that waits forever."""
    monkeypatch.setenv(TOKEN_VARIABLE, "xoxb-test")
    monkeypatch.setenv(SECRET_VARIABLE, "shh")
    monkeypatch.setenv(missing, "  ")

    def never(*_: Any, **__: Any) -> None:
        raise AssertionError("the receiver was built without credentials")

    monkeypatch.setattr(slack_cli, "build_app", never)

    assert _run(url, "serve") == 1
    assert "must both be set" in capsys.readouterr().err


def test_serve_starts_the_receiver_on_the_requested_port(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(TOKEN_VARIABLE, "xoxb-test")
    monkeypatch.setenv(SECRET_VARIABLE, "shh")
    started: list[int] = []

    class App:
        def start(self, port: int) -> None:
            started.append(port)

    built: dict[str, Any] = {}

    def build_app(*_: Any, **kwargs: Any) -> App:
        built.update(kwargs)
        return App()

    monkeypatch.setattr(slack_cli, "build_app", build_app)

    assert _run(url, "serve", "--port", "3456") == 0
    assert started == [3456]
    assert built == {"signing_secret": "shh", "token": "xoxb-test"}
    assert ":3456/slack/events" in capsys.readouterr().out
