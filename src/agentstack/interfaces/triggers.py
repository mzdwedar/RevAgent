"""The event ingress: where a trigger enters the system (Interfaces & channels).

A trigger is an inbound event, and this layer does with it exactly what the channel
layer does with a Slack message: checks that it is well formed and hands it on. It
resolves no identity, reads no policy and decides nothing - the tenant on a trigger is
a **claim**, not an authorisation, and layer 8 is where that claim is tested.

The one thing worth saying about validation here: an unknown `kind` is refused rather
than defaulted. Defaulting would mean an unrecognised trigger silently acquiring
whichever authority the default carries, and the only default that could be safe is the
one that can do least - at which point a typo downgrades a real trigger instead.
"""

from __future__ import annotations

from typing import Any

from agentstack.policy.triggers import TriggerEvent, TriggerKind

MAX_IDENTIFIER = 200


class MalformedTrigger(ValueError):
    """The event is not a trigger this system can act on."""


def parse_trigger(payload: dict[str, Any], *, source: str = "unknown") -> TriggerEvent:
    """Turn an untrusted payload into a trigger, or refuse it."""
    kind_value = _required(payload, "kind")
    try:
        kind = TriggerKind(kind_value)
    except ValueError as exc:
        known = ", ".join(sorted(k.value for k in TriggerKind))
        raise MalformedTrigger(
            f"{kind_value!r} is not a trigger kind; known kinds are {known}. "
            "An unknown kind is refused rather than defaulted: a default would hand a "
            "typo whichever authority the default carries."
        ) from exc

    return TriggerEvent(
        kind=kind,
        experiment_id=_required(payload, "experiment_id"),
        # The watermark of the batch that fired this trigger. It is what makes the
        # cycle idempotent and what an approval is later bound against, so a trigger
        # without one cannot be acted on at all.
        data_as_of=_required(payload, "data_as_of"),
        tenant=_required(payload, "tenant"),
        source=source,
    )


def _required(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MalformedTrigger(f"a trigger needs a non-empty {field!r}; got {value!r}")
    if len(value) > MAX_IDENTIFIER:
        raise MalformedTrigger(
            f"{field!r} is {len(value)} characters; this is an identifier, not a payload"
        )
    return value.strip()
