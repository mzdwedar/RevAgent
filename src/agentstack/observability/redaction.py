"""What a span may carry out of the process (Observability, evaluation, feedback).

Traces are debug evidence kept somewhere with looser access than the audit sink, and
they outlive the run. The rule is an allowlist: an attribute is exported only if its
name is listed here, so a new attribute is a visible change to this file rather than a
field that quietly starts shipping customer data.

Two layers, and they are not equal. The allowlist is the guarantee. The text rules below
it are defence in depth for the one kind of attribute that holds prose (`reason`,
which can quote model output or a validation error): they truncate, and they mask what
looks like an address, a long number or a token, plus any secret this process has
revealed (`storage.secrets.mask`). A pattern is a net, not a proof.

`tests/fitness/test_span_attributes.py` holds every `tracer.span(...)` call site in `src`
to this list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from agentstack.storage.secrets import mask

# Attribute names that may leave the process. Identifiers and counts, never a row.
SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "allowed",
        "asked",
        "blocked_on",
        "claimed_at",
        "data_as_of",
        "decision",
        "deduplicated",
        "described",
        "dropped_out_of_scope",
        "experiment",
        "fingerprint",
        "items",
        "kind",
        "outcome",
        "proposals",
        "reason",
        "receipts",
        "refusal",
        "refusals",
        "request_fingerprint",
        "resource",
        "resumed_by",
        "rule",
        "stage",
        "status",
        "surface",
        "tenant",
        "tier",
        "tool",
        "tools",
        "untrusted",
        "wait",
        "waits",
    }
)

# Attributes whose value is prose, so a person or a model chose the words.
FREE_TEXT: frozenset[str] = frozenset({"reason"})

MAX_FREE_TEXT = 160

_PATTERNS = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b(?:xox[a-z]-|sk-|ghp_)[\w-]{6,}"), "<token>"),
    (re.compile(r"\d{7,}"), "<number>"),
)


def scrub_text(text: str) -> str:
    """Prose with secrets, addresses and long numbers masked, on one bounded line."""
    text = mask(text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_FREE_TEXT else text[: MAX_FREE_TEXT - 1] + "…"


def _scrub_value(value: Any, *, free_text: bool) -> Any:
    if isinstance(value, str):
        return scrub_text(value) if free_text else mask(value)
    if isinstance(value, list | tuple):
        return [_scrub_value(item, free_text=free_text) for item in value]
    return value


def scrub(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """The attributes that may be exported, and the names of any that were dropped.

    A dropped name is reported (`dropped_attributes`), because silently losing a field
    would make a trace look complete when it is not. Only the name is kept: the value is
    the thing that was not allowed out.
    """
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for name, value in attributes.items():
        if name in SPAN_ATTRIBUTES:
            kept[name] = _scrub_value(value, free_text=name in FREE_TEXT)
        else:
            dropped.append(name)
    if dropped:
        kept["dropped_attributes"] = sorted(dropped)
    return kept
