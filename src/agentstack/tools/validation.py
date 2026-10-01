"""Validating model-proposed arguments against the declared schema (Tools, MCP, capability
surfaces).

A schema validates shape. That is a small claim, but it only holds if something
actually checks it: an `input_schema` that is never consulted validates nothing, and
the coercion then happens ad hoc inside each prepare() function, where a missing field
is a KeyError and a garbage number is a ValueError - both of which escape the turn
uncaught.

This is a deliberately small subset of JSON Schema: object types, `required`,
`properties` with primitive type names, `enum`, `minimum`/`maximum`, `format`, and
`additionalProperties` defaulting to false. That is what the tool catalog declares.
It is not a dependency because the subset a capability surface needs is this small -
if a tool ever needs `oneOf` or `$ref`, reach for jsonschema then and say so in an ADR.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from agentstack.tools.spec import ToolSpec

_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


# One segment of a resource path: starts alphanumeric, then letters, digits and `._:-`.
# No `/`, so an id cannot add a segment - and the registry takes the act a resource
# names from its last segment, so `experiment_id=exp-9/rollout` would otherwise turn a
# draft into a rollout under the draft tool's grant (C1). No leading `.`, so `..` is
# not an id either. Bounded, because an id is a key, not a document.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


# Every `format` the catalog declares, and what a value must be to meet it. A format
# not in this table is refused rather than waved through: a declared constraint nothing
# enforces is exactly what `format: id` was (C1). `tests/fitness/test_request_binding.py`
# holds every declared format to this table, so the refusal never fires on a tool that
# ships - it fires on the one that would have shipped unchecked.
_FORMATS: dict[str, tuple[Callable[[Any], bool], str]] = {
    "id": (_is_id, "a single id: letters, digits and ._:- with no '/'"),
}


def known_formats() -> frozenset[str]:
    """The formats this validator enforces. Read by the fitness suite."""
    return frozenset(_FORMATS)


class InvalidToolArguments(ValueError):
    """The model proposed a call that does not match the tool's declared shape.

    A refusal, not a crash: the runtime catches it, records it, and answers.
    """


def validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Check `arguments` against `spec.input_schema` and return them, or refuse."""
    schema = spec.input_schema
    if schema.get("type") != "object":
        raise InvalidToolArguments(f"{spec.name}: only object schemas are supported")

    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    missing = [name for name in required if name not in arguments]
    if missing:
        raise InvalidToolArguments(f"{spec.name}: missing required argument(s) {missing}")

    if not schema.get("additionalProperties", False):
        unexpected = [name for name in arguments if name not in properties]
        if unexpected:
            raise InvalidToolArguments(
                f"{spec.name}: unexpected argument(s) {unexpected}; a narrow tool is "
                "narrow only if its arguments are too"
            )

    coerced: dict[str, Any] = {}
    for name, value in arguments.items():
        declared = properties.get(name, {}).get("type")
        expected = _TYPES.get(declared) if declared else None
        if expected is None:
            coerced[name] = value
            continue
        # Transport is stringly-typed (a model emits text, a form posts strings), so a
        # string that *is* the declared primitive is accepted and converted. A string
        # that is not one is a refusal, never a best-effort guess.
        if isinstance(value, str) and expected is not str:
            try:
                value = _from_text(declared, value)
            except ValueError as exc:
                raise InvalidToolArguments(
                    f"{spec.name}: {name} must be {declared}, got {value!r}"
                ) from exc
        if declared == "integer" and isinstance(value, bool):
            raise InvalidToolArguments(f"{spec.name}: {name} must be integer, got a boolean")
        if not isinstance(value, expected):
            raise InvalidToolArguments(
                f"{spec.name}: {name} must be {declared}, got {type(value).__name__}"
            )
        _within_bounds(spec, name, properties[name], value)
        coerced[name] = value
    return coerced


def _within_bounds(spec: ToolSpec, name: str, declared: Mapping[str, Any], value: Any) -> None:
    """`enum`, `minimum`, `maximum` and `format`: a bound a schema declares and nothing
    enforces is a budget in name only - `limit: 5000` would reach the surface looking
    validated, and so would `experiment_id: exp-9/rollout`."""
    fmt = declared.get("format")
    if fmt is not None:
        if fmt not in _FORMATS:
            raise InvalidToolArguments(
                f"{spec.name}: {name} declares format {fmt!r}, which nothing enforces"
            )
        meets, meaning = _FORMATS[fmt]
        if not meets(value):
            raise InvalidToolArguments(f"{spec.name}: {name} must be {meaning}, got {value!r}")
    allowed = declared.get("enum")
    if allowed is not None and value not in allowed:
        raise InvalidToolArguments(f"{spec.name}: {name} must be one of {allowed}, got {value!r}")
    low, high = declared.get("minimum"), declared.get("maximum")
    if low is not None and value < low:
        raise InvalidToolArguments(f"{spec.name}: {name} must be at least {low}, got {value}")
    if high is not None and value > high:
        raise InvalidToolArguments(f"{spec.name}: {name} must be at most {high}, got {value}")


def _from_text(declared: str, value: str) -> Any:
    if declared == "integer":
        return int(value)
    if declared == "number":
        return float(value)
    if declared == "boolean":
        lowered = value.strip().lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        raise ValueError(value)
    raise ValueError(value)
