"""Slack's answer coming back, and proving it is Slack's (Interfaces & channels).

Three checks, and each catches something the others do not:

* **Signature** over the raw body. Proves the bytes came from Slack.
* **Timestamp freshness**. Proves they are recent, so a captured request cannot be
  useful forever.
* **Replay**. Proves this is the first time. A signature says a payload is authentic;
  it says nothing about how many times it arrived, and the second copy of an "Approve"
  is not a second decision.

All three run here, before any policy - criterion 24 - because an interaction that
cannot be shown to have come from Slack should never reach code that reasons about who
may approve.

**The user id is a claim.** This adapter reads it off the payload and passes it on. It
has no opinion about who may approve; that is layer 8's decision (criterion 25), and an
adapter that filtered on it would be authorising in the place least able to audit it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from agentstack.policy.approvers import ApprovalReply
from agentstack.storage.database import Database

SECRET_VARIABLE = "SLACK_SIGNING_SECRET"
SIGNATURE_VERSION = "v0"

# Slack's own recommendation. Long enough for clock skew, short enough that a captured
# request stops being useful quickly.
MAX_AGE_SECONDS = 300

APPROVE_ACTION = "approve_rollout"
REFUSE_ACTION = "refuse_rollout"


class CallbackRefused(PermissionError):
    """The interaction cannot be shown to be a first-time message from Slack."""


class BadSignature(CallbackRefused):
    """The bytes are not signed by the secret we share with Slack."""


class StaleTimestamp(CallbackRefused):
    """Signed, but too old to act on."""


class ReplayedDelivery(CallbackRefused):
    """Signed and fresh, and we have seen it before."""


class MalformedCallback(ValueError):
    """Authentic, and not an interaction this system understands."""


def signing_secret(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    secret = source.get(SECRET_VARIABLE, "").strip()
    if not secret:
        raise CallbackRefused(
            f"{SECRET_VARIABLE} is not set, so no interaction can be shown to come from "
            "Slack. Refusing every callback is the correct behaviour here - accepting "
            "unverified ones would make the approval boundary decorative."
        )
    return secret


def expected_signature(secret: str, *, sent_at: str, raw_body: bytes) -> str:
    """Slack's v0 scheme, over the **raw** bytes.

    Not over a re-serialised parse. Re-encoding changes the bytes - key order, spacing,
    unicode escaping - so the digest would either fail for authentic messages or, far
    worse, be computed over something other than what was parsed.
    """
    basestring = b"%s:%s:%s" % (SIGNATURE_VERSION.encode(), sent_at.encode(), raw_body)
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


@dataclass(frozen=True, slots=True)
class ReplayGuard:
    """Remembers which deliveries have been accepted.

    In Postgres rather than in memory: a replay that lands on a different worker than
    the original is exactly the case this exists for, and a per-process set would wave
    it through.
    """

    db: Database

    def claim(self, *, signature: str, sent_at: str) -> bool:
        """True when this delivery is being seen for the first time."""
        row = self.db.fetch_one(
            "INSERT INTO slack_deliveries (signature, sent_at) VALUES (%s, %s)"
            " ON CONFLICT (signature) DO UPDATE SET signature = EXCLUDED.signature"
            " RETURNING (xmax = 0) AS inserted",
            (signature, sent_at),
        )
        assert row is not None  # the upsert always returns exactly one row
        return bool(row[0])

    def prune(self, *, older_than_seconds: int = MAX_AGE_SECONDS) -> int:
        """Drop deliveries too old to be replayable.

        A row outside the freshness window cannot be replayed anyway - the timestamp
        check refuses it first - so keeping it forever only grows the table.
        """
        rows = self.db.fetch_all(
            "DELETE FROM slack_deliveries"
            " WHERE received_at < now() - make_interval(secs => %s) RETURNING signature",
            (older_than_seconds,),
        )
        return len(rows)


def verify(
    *,
    raw_body: bytes,
    sent_at: str,
    signature: str,
    guard: ReplayGuard,
    secret: str | None = None,
    now: float | None = None,
) -> None:
    """Prove the interaction is a first-time message from Slack, or refuse it.

    Freshness first, because it is cheap and drops stale floods without hashing.
    Signature second. **Replay last**, on purpose: recording an unverified signature
    would let anyone fill the table by posting garbage, and the guard would then be a
    denial-of-service surface rather than a defence.
    """
    try:
        age = abs((now if now is not None else time.time()) - float(sent_at))
    except (TypeError, ValueError) as exc:
        raise StaleTimestamp(f"{sent_at!r} is not a timestamp") from exc
    if age > MAX_AGE_SECONDS:
        raise StaleTimestamp(
            f"the interaction is {age:.0f}s old and the window is {MAX_AGE_SECONDS}s"
        )

    expected = expected_signature(secret or signing_secret(), sent_at=sent_at, raw_body=raw_body)
    if not hmac.compare_digest(expected, signature):
        raise BadSignature("the interaction is not signed by the secret Slack shares with us")

    if not guard.claim(signature=signature, sent_at=sent_at):
        raise ReplayedDelivery(
            "this interaction has already been accepted; the second copy of an approval "
            "is not a second decision"
        )


def form_field(raw_body: bytes, name: str) -> str:
    """One field out of an `application/x-www-form-urlencoded` body.

    Hand-rolled rather than `urllib.parse.parse_qs`, and not by preference.
    `lint-imports` forbids `urllib` to this layer because the rule it enforces - only
    the execution gateway touches a network client - cannot be written as
    "`urllib.request` but not `urllib.parse`": import-linter rejects submodules of
    external packages. Widening the rule to admit a URL *parser* would weaken a real
    constraint for a tooling limitation, so the decoding is here instead, where it is
    ten lines and covered by tests.
    """
    for pair in raw_body.split(b"&"):
        key, separator, value = pair.partition(b"=")
        if separator and key.decode("ascii", errors="replace") == name:
            return _percent_decode(value.replace(b"+", b" "))
    return ""


def _percent_decode(value: bytes) -> str:
    out = bytearray()
    index = 0
    while index < len(value):
        byte = value[index]
        if byte == ord("%") and index + 2 < len(value) + 1:
            try:
                out.append(int(value[index + 1 : index + 3], 16))
            except ValueError as exc:
                raise MalformedCallback(
                    f"{value[index : index + 3]!r} is not a percent-escape"
                ) from exc
            index += 3
            continue
        out.append(byte)
        index += 1
    return out.decode("utf-8", errors="replace")


def parse_interaction(raw_body: bytes) -> ApprovalReply:
    """Read Slack's block-action payload into a reply bound to what was asked."""
    encoded = form_field(raw_body, "payload")
    if not encoded:
        raise MalformedCallback("the interaction carries no payload")
    try:
        payload: dict[str, Any] = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise MalformedCallback(f"the payload is not JSON: {exc}") from exc

    actions = payload.get("actions") or []
    if not actions:
        raise MalformedCallback("the interaction names no action")
    action = actions[0]
    action_id = action.get("action_id")
    if action_id not in {APPROVE_ACTION, REFUSE_ACTION}:
        raise MalformedCallback(f"{action_id!r} is not an approval action")

    binding = str(action.get("value") or "")
    parts = binding.split("|")
    if len(parts) != 4 or not all(parts):
        raise MalformedCallback(
            f"{binding!r} does not bind a run, a wait and a frozen cohort; an answer "
            "that cannot be matched to a question is not an answer"
        )

    user = (payload.get("user") or {}).get("id")
    if not user:
        raise MalformedCallback("the interaction names no user")

    run_id, wait_id, experiment_version, data_as_of = parts
    return ApprovalReply(
        run_id=run_id,
        wait_id=wait_id,
        experiment_version=experiment_version,
        data_as_of=data_as_of,
        approved=action_id == APPROVE_ACTION,
        # A claim. Nothing here has checked whether this person may approve.
        slack_user_id=str(user),
        channel=str((payload.get("channel") or {}).get("id") or ""),
    )


def accept(
    *,
    raw_body: bytes,
    sent_at: str,
    signature: str,
    guard: ReplayGuard,
    secret: str | None = None,
    now: float | None = None,
) -> ApprovalReply:
    """Verify, then read. Never the other way round.

    Parsing first would mean acting on the shape of bytes nobody has authenticated, and
    every field read out of them would be attacker-controlled input that had already
    been treated as structure.
    """
    verify(
        raw_body=raw_body,
        sent_at=sent_at,
        signature=signature,
        guard=guard,
        secret=secret,
        now=now,
    )
    return parse_interaction(raw_body)
