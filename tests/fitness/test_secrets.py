"""Infrastructure substrate: a secret enters through one door and does not print.

Secrets were read from the environment wherever they were needed. These hold the seam
that replaced that: one module reads them, a `Secret` refuses to print, and a failure
names the variable and never the value.
"""

from __future__ import annotations

import ast
import json
import pickle
from pathlib import Path

import pytest

from agentstack.interfaces import slack, slack_callback
from agentstack.prediction import licence
from agentstack.storage import secrets
from agentstack.storage.pool import redacted

SRC = Path(__file__).resolve().parents[2] / "src" / "agentstack"
VALUE = "s3cret-value-123"

# Where the environment may be read, and why. A secret is never one of these.
NOT_SECRETS = {
    "storage/secrets.py": "the seam itself",
    "runtime/temporal/client.py": "TEMPORAL_ADDRESS is a hostname, not a credential",
    "prediction/licence.py": "load_env fills the environment from .env; it reads no secret",
}


@pytest.fixture(autouse=True)
def _clean() -> object:
    secrets.forget_revealed()
    yield None
    secrets.forget_revealed()


def test_a_secret_prints_its_name_and_never_its_value() -> None:
    secret = secrets.Secret("TOKEN", VALUE)

    renderings = (
        str(secret),
        repr(secret),
        f"{secret}",
        format(secret, ">20"),
    )
    for rendered in renderings:
        assert VALUE not in rendered
        assert "TOKEN" in rendered
    assert VALUE not in json.dumps({"x": str(secret)})


def test_a_secret_cannot_be_serialised() -> None:
    with pytest.raises(TypeError, match="not serialised"):
        pickle.dumps(secrets.Secret("TOKEN", VALUE))


def test_a_blank_or_missing_secret_is_not_set() -> None:
    assert secrets.read("X", env={}) is None
    assert secrets.read("X", env={"X": "   "}) is None
    padded = secrets.read("X", env={"X": f"  {VALUE} "})
    assert padded is not None
    assert padded.reveal() == VALUE


def test_requiring_a_missing_secret_names_the_variable() -> None:
    with pytest.raises(secrets.SecretMissing, match="X is not set"):
        secrets.require("X", env={})


def test_reading_follows_the_environment_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_SECRET", VALUE)

    assert secrets.require("SOME_SECRET").reveal() == VALUE
    monkeypatch.delenv("SOME_SECRET")
    assert secrets.read("SOME_SECRET") is None


def test_mask_removes_only_what_was_revealed_and_only_long_values() -> None:
    secrets.require("LONG", env={"LONG": VALUE}).reveal()
    secrets.require("SHORT", env={"SHORT": "abc"}).reveal()

    assert secrets.mask(f"a {VALUE} b abc") == "a <secret:LONG> b abc"


def test_a_secret_that_was_never_revealed_is_not_masked() -> None:
    secrets.require("UNUSED", env={"UNUSED": VALUE})

    assert secrets.mask(f"{VALUE}") == VALUE


def test_a_licence_refusal_names_the_variable_not_the_value() -> None:
    with pytest.raises(licence.LicenceRefused) as short:
        licence.check_token({licence.TOKEN_VARIABLE: "tiny"})
    with pytest.raises(licence.LicenceRefused) as missing:
        licence.check_token({})

    assert "tiny" not in str(short.value)
    assert licence.TOKEN_VARIABLE in str(missing.value)


def test_a_missing_signing_secret_refuses_without_a_value() -> None:
    with pytest.raises(slack_callback.CallbackRefused, match="SLACK_SIGNING_SECRET"):
        slack_callback.signing_secret({})


def test_the_database_url_prints_without_its_password() -> None:
    printed = redacted("postgresql://agent:hunter2-pass@db:5432/agentstack")

    assert "hunter2-pass" not in printed


def test_only_the_seam_reads_the_environment() -> None:
    """The set of modules that read it is small and named, so a new secret cannot arrive
    through `os.environ` without this file, and its reason, changing."""
    readers = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            text = ast.unparse(node) if isinstance(node, ast.Attribute) else ""
            if text in {"os.environ", "os.getenv", "os.environb"}:
                readers.add(str(path.relative_to(SRC)))

    assert readers <= set(NOT_SECRETS), (
        f"read the environment outside the secrets seam: {sorted(readers - set(NOT_SECRETS))}"
    )


def test_a_present_signing_secret_is_returned_for_the_signature_check() -> None:
    assert slack_callback.signing_secret({"SLACK_SIGNING_SECRET": VALUE}) == VALUE


def test_the_slack_client_is_built_from_the_token_and_the_token_stays_out_of_its_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(slack.TOKEN_VARIABLE, "xoxb-test-value-123")

    client = slack.SlackNotifier()._client()

    assert type(client).__name__ == "WebClient"
    assert "xoxb-test-value-123" not in repr(secrets.read(slack.TOKEN_VARIABLE))
