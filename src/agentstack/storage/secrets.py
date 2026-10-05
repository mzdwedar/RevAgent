"""The one place a secret is read (Infrastructure substrate).

Secrets were read from the environment wherever they were needed, so nothing could say
which values the process held, and nothing could stop one reaching a log. This module is
the seam: every secret enters through `read`, arrives as a `Secret` that refuses to print,
and is remembered so `mask` can take its value out of any text on the way to a sink.

It is a seam, not a store. The source is still the process environment (ADR-0013); moving
to a secret manager means a different `SecretSource` here and nowhere else. Environment
variables are not a secret store, and this does not make them one. What it buys is that
the move touches one file, and that a leak into a trace is caught by a test.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol

# A value shorter than this is masked nowhere: replacing every "a" in a log line would
# destroy it, and no real token is this short.
MINIMUM_MASKED_LENGTH = 8

# Every value `reveal` has handed out, by name. Process memory already holds these; the
# registry only lets the redactor find them again.
_revealed: dict[str, str] = {}


class SecretMissing(RuntimeError):
    """A required secret is not set. Names the variable, never a value."""


class SecretSource(Protocol):
    def get(self, name: str) -> str | None: ...


class EnvironmentSource:
    """The process environment, read at call time so a test can change it."""

    def get(self, name: str) -> str | None:
        return os.environ.get(name)


class Secret:
    """A value that does not print.

    `str`, `repr`, `format` and an f-string all give the name and nothing else, so
    interpolating one into a message or a span is harmless. The value leaves only through
    `reveal()`, which is greppable and is the single place a call site says "I mean it".
    """

    __slots__ = ("_value", "name")

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self._value = value

    def reveal(self) -> str:
        if len(self._value) >= MINIMUM_MASKED_LENGTH:
            _revealed[self.name] = self._value
        return self._value

    def __repr__(self) -> str:
        return f"Secret({self.name}=<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self) -> Any:
        raise TypeError(f"{self.name} is a secret and is not serialised")

    def __bool__(self) -> bool:
        return bool(self._value)


def read(
    name: str, *, env: Mapping[str, str] | None = None, source: SecretSource | None = None
) -> Secret | None:
    """The secret called `name`, or None when it is unset or blank.

    `env` is for the callers that already took a mapping so a test can supply one.
    """
    chosen = source or EnvironmentSource()
    raw = env.get(name) if env is not None else chosen.get(name)
    value = (raw or "").strip()
    return Secret(name, value) if value else None


def require(
    name: str, *, env: Mapping[str, str] | None = None, source: SecretSource | None = None
) -> Secret:
    found = read(name, env=env, source=source)
    if found is None:
        raise SecretMissing(f"{name} is not set")
    return found


def mask(text: str) -> str:
    """`text` with every revealed secret's value replaced by `<secret:NAME>`."""
    for name, value in _revealed.items():
        text = text.replace(value, f"<secret:{name}>")
    return text


def forget_revealed() -> None:
    """For tests: a secret one test revealed must not mask another test's text."""
    _revealed.clear()
