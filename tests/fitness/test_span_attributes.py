"""Observability, evaluation, feedback: a span exports only what is listed.

Traces sit somewhere with looser access than the audit sink and outlive the run, so what
a span may carry is an allowlist (`redaction.SPAN_ATTRIBUTES`). This holds every call site
in `src` to it, so a new attribute is a deliberate edit to that list and not a field that
quietly starts shipping customer data - and holds the sink to dropping what is not listed.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from agentstack.observability.redaction import (
    FREE_TEXT,
    MAX_FREE_TEXT,
    SPAN_ATTRIBUTES,
    scrub,
    scrub_text,
)
from agentstack.observability.spans import LoggingSink, Span, VersionStamp
from agentstack.storage import secrets

SRC = Path(__file__).resolve().parents[2] / "src" / "agentstack"
STAMP = VersionStamp(prompt="p", model="m", tool_schema="t", policy="y", retrieval="r")


def attributes_used() -> dict[str, set[str]]:
    """attribute name -> where it is set, from `.span(name, k=v)` and `.attributes.update(k=v)`."""
    used: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            is_span = node.func.attr == "span"
            is_update = node.func.attr == "update" and "attributes" in ast.unparse(node.func.value)
            if not (is_span or is_update):
                continue
            for keyword in node.keywords:
                name = keyword.arg or "**"
                used.setdefault(name, set()).add(f"{path.relative_to(SRC)}:{node.lineno}")
    return used


def test_every_attribute_a_span_is_given_is_on_the_allowlist() -> None:
    unlisted = {k: v for k, v in attributes_used().items() if k not in SPAN_ATTRIBUTES}

    assert not unlisted, (
        f"span attributes not on the allowlist: {unlisted}. Add the name to "
        "observability/redaction.py only if it can never hold a customer's data."
    )


def test_the_allowlist_holds_nothing_no_span_uses() -> None:
    """A list that only grows stops being a decision."""
    assert set(attributes_used()) >= SPAN_ATTRIBUTES, (
        f"allowlisted but unused: {sorted(SPAN_ATTRIBUTES - set(attributes_used()))}"
    )


def test_no_span_is_given_attributes_by_splat() -> None:
    """`**anything` is an attribute name nobody reviewed."""
    assert "**" not in attributes_used()


def test_free_text_is_a_subset_of_the_allowlist() -> None:
    assert FREE_TEXT <= SPAN_ATTRIBUTES


def test_an_unlisted_attribute_is_dropped_and_its_name_is_reported() -> None:
    scrubbed = scrub({"tool": "t", "monthly_charge": 79.5, "email": "a@b.co"})

    assert scrubbed == {"tool": "t", "dropped_attributes": ["email", "monthly_charge"]}


def test_a_dropped_value_appears_nowhere_in_what_is_exported() -> None:
    scrubbed = scrub({"salary": 987654.321, "tool": "t"})

    assert "987654" not in json.dumps(scrubbed)


def test_free_text_is_truncated_to_one_bounded_line() -> None:
    text = scrub_text("line one\nline two " + "x" * 500)

    assert "\n" not in text
    assert len(text) <= MAX_FREE_TEXT


@pytest.mark.parametrize(
    ("said", "must_not_contain"),
    [
        ("could not reach ana@acme.example about it", "ana@acme.example"),
        ("customer 7001234567 has no plan", "7001234567"),
        ("bad token xoxb-1234567890-abcdef", "xoxb-1234567890"),
    ],
)
def test_prose_has_addresses_numbers_and_tokens_masked(said: str, must_not_contain: str) -> None:
    assert must_not_contain not in scrub_text(said)


def test_a_revealed_secret_is_masked_wherever_it_appears() -> None:
    secrets.forget_revealed()
    value = secrets.require("X", env={"X": "s3cret-value-123"}).reveal()

    scrubbed = scrub({"reason": f"failed with {value}", "tool": value})

    assert value not in json.dumps(scrubbed)
    assert "<secret:X>" in scrubbed["reason"]
    secrets.forget_revealed()


def test_the_logging_sink_exports_only_scrubbed_attributes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets.forget_revealed()
    token = secrets.require("SLACK_BOT_TOKEN", env={"SLACK_BOT_TOKEN": "xoxb-live-value-999"})
    span = Span(
        name="tool.reject",
        run_id="r",
        session_id="s",
        versions=STAMP,
        attributes={
            "tool": "create_draft",
            "reason": f"rejected: {token.reveal()} for ana@acme.example",
            "monthly_charge": 79.5,
        },
    )

    with caplog.at_level(logging.INFO, logger="agentstack.traces"):
        LoggingSink().export([span])

    line = caplog.records[0].getMessage()
    exported = json.loads(line)["attributes"]
    assert "xoxb-live-value-999" not in line
    assert "ana@acme.example" not in line
    assert "79.5" not in line
    assert exported["dropped_attributes"] == ["monthly_charge"]
    assert exported["tool"] == "create_draft"
    secrets.forget_revealed()
