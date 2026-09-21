"""Validating model-proposed arguments against the declared schema (Part 6).

A schema validates shape. That is a small claim, but it only holds if something
actually checks it: an `input_schema` that is never consulted validates nothing, and
the coercion then happens ad hoc inside each prepare() function, where a missing field
is a KeyError and a garbage number is a ValueError - both of which escape the turn
uncaught.

This is a deliberately small subset of JSON Schema: object types, `required`,
`properties` with primitive type names, and `additionalProperties` defaulting to
false. That is what the tool catalog declares. It is not a dependency because the
subset a capability surface needs is this small - if a tool ever needs `oneOf` or
`$ref`, reach for jsonschema then and say so in an ADR.
"""

from __future__ import annotations

from collections.abc import Mapping
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
        coerced[name] = value
    return coerced


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
