"""Criterion 24: a forged approval never reaches the approval store.

A bad signature, a stale timestamp, or a replayed body is refused at the adapter,
before any policy runs. Three checks, each catching what the others do not: a signature
proves the bytes came from Slack, freshness proves they are recent, and the replay
guard proves this is the first time. The second copy of an "Approve" is not a second
decision.

Criterion 25 is the other half and belongs to T16: the adapter reads the user id and
has no opinion about who may approve.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from urllib.parse import parse_qs, urlencode

import pytest

from agentstack.interfaces import slack_callback
from agentstack.interfaces.slack_callback import (
    MAX_AGE_SECONDS,
    ApprovalReply,
    BadSignature,
    CallbackRefused,
    MalformedCallback,
    ReplayedDelivery,
    ReplayGuard,
    StaleTimestamp,
    accept,
    expected_signature,
    parse_interaction,
)
from agentstack.storage.database import Database

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
BINDING = "run-7|wait-1|exp:5cbf2762|telecom-bigml:f107d488"


def body(action: str = "approve_rollout", value: str = BINDING, user: str = "U123") -> bytes:
    payload = {
        "type": "block_actions",
        "user": {"id": user, "name": "ana"},
        "channel": {"id": "C999"},
        "actions": [{"action_id": action, "value": value, "type": "button"}],
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


def signed(raw: bytes, sent_at: str | None = None) -> tuple[bytes, str, str]:
    stamp = sent_at or str(int(time.time()))
    return raw, stamp, expected_signature(SECRET, sent_at=stamp, raw_body=raw)


@pytest.fixture
def guard(app_database: Database) -> ReplayGuard:
    return ReplayGuard(db=app_database)


# --- the happy path, so the refusals below mean something ---


def test_a_genuine_approval_is_accepted(guard: ReplayGuard) -> None:
    raw, sent_at, signature = signed(body())

    reply = accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)

    assert reply.approved is True
    assert reply.run_id == "run-7"
    assert reply.wait_id == "wait-1"
    assert reply.experiment_version == "exp:5cbf2762"
    assert reply.data_as_of == "telecom-bigml:f107d488"
    assert reply.slack_user_id == "U123"


def test_a_refusal_is_carried_as_a_refusal(guard: ReplayGuard) -> None:
    """A prompt with two buttons needs both answers to arrive as themselves."""
    raw, sent_at, signature = signed(body(action="refuse_rollout"))

    reply = accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)

    assert reply.approved is False


# --- signature ---


def test_a_forged_signature_is_refused(guard: ReplayGuard) -> None:
    raw, sent_at, _ = signed(body())

    with pytest.raises(BadSignature):
        accept(
            raw_body=raw,
            sent_at=sent_at,
            signature="v0=" + "0" * 64,
            guard=guard,
            secret=SECRET,
        )


def test_a_signature_from_a_different_secret_is_refused(guard: ReplayGuard) -> None:
    raw, sent_at, _ = signed(body())
    theirs = expected_signature("not-our-secret", sent_at=sent_at, raw_body=raw)

    with pytest.raises(BadSignature):
        accept(raw_body=raw, sent_at=sent_at, signature=theirs, guard=guard, secret=SECRET)


def test_changing_one_byte_of_the_body_invalidates_it(guard: ReplayGuard) -> None:
    """The point of signing the body rather than a header."""
    raw, sent_at, signature = signed(body())
    tampered = raw.replace(b"U123", b"U666")

    with pytest.raises(BadSignature):
        accept(raw_body=tampered, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)


def test_a_reserialised_payload_does_not_carry_the_original_signature() -> None:
    """Why the digest is over the raw bytes rather than over a parse.

    These two bodies mean the same thing and are different bytes - JSON key order and
    separators are not part of the meaning. Verifying a re-serialised parse would
    either reject authentic messages or, far worse, compute the digest over something
    other than what was parsed.
    """
    original = urlencode({"payload": json.dumps({"a": 1, "b": 2})}).encode()
    equivalent = urlencode(
        {"payload": json.dumps({"b": 2, "a": 1}, separators=(", ", ": "))}
    ).encode()

    assert json.loads(parse_qs(original.decode())["payload"][0]) == json.loads(
        parse_qs(equivalent.decode())["payload"][0]
    ), "the two bodies carry the same meaning"
    assert expected_signature(SECRET, sent_at="1", raw_body=original) != expected_signature(
        SECRET, sent_at="1", raw_body=equivalent
    ), "and different bytes, so only the raw ones can be verified"


def test_the_timestamp_is_part_of_what_is_signed() -> None:
    """Otherwise a captured body could be re-dated to stay fresh forever."""
    raw = body()

    assert expected_signature(SECRET, sent_at="1000", raw_body=raw) != expected_signature(
        SECRET, sent_at="2000", raw_body=raw
    )


# --- freshness ---


def test_a_stale_interaction_is_refused(guard: ReplayGuard) -> None:
    old = str(int(time.time()) - MAX_AGE_SECONDS - 1)
    raw, sent_at, signature = signed(body(), sent_at=old)

    with pytest.raises(StaleTimestamp, match="window"):
        accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)


def test_an_interaction_from_the_future_is_refused_too(guard: ReplayGuard) -> None:
    """Clock skew cuts both ways, and a far-future timestamp would otherwise be fresh
    for as long as the attacker chose."""
    ahead = str(int(time.time()) + MAX_AGE_SECONDS + 60)
    raw, sent_at, signature = signed(body(), sent_at=ahead)

    with pytest.raises(StaleTimestamp):
        accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)


def test_a_timestamp_that_is_not_a_number_is_refused(guard: ReplayGuard) -> None:
    raw, _, signature = signed(body())

    with pytest.raises(StaleTimestamp, match="not a timestamp"):
        accept(raw_body=raw, sent_at="yesterday", signature=signature, guard=guard, secret=SECRET)


# --- replay ---


def test_the_same_interaction_twice_is_refused_the_second_time(guard: ReplayGuard) -> None:
    """A signature proves a payload is authentic. It says nothing about how many times
    it arrived."""
    raw, sent_at, signature = signed(body())

    accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)

    with pytest.raises(ReplayedDelivery, match="not a second decision"):
        accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)


def test_the_replay_guard_is_shared_not_per_process(app_database: Database) -> None:
    """A replay landing on a different worker than the original is the case this
    exists for. A per-process set would wave it through."""
    raw, sent_at, signature = signed(body())
    one = ReplayGuard(db=app_database)
    another = ReplayGuard(db=app_database)

    accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=one, secret=SECRET)

    with pytest.raises(ReplayedDelivery):
        accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=another, secret=SECRET)


def test_two_copies_arriving_together_produce_one_acceptance(guard: ReplayGuard) -> None:
    raw, sent_at, signature = signed(body())
    accepted: list[object] = []
    barrier = threading.Barrier(4)

    def deliver() -> None:
        barrier.wait()
        try:
            accepted.append(
                accept(
                    raw_body=raw,
                    sent_at=sent_at,
                    signature=signature,
                    guard=guard,
                    secret=SECRET,
                )
            )
        except CallbackRefused as exc:
            accepted.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    winners = [a for a in accepted if isinstance(a, ApprovalReply)]
    assert len(winners) == 1, f"{len(winners)} copies of one approval were accepted"


def test_an_unverified_signature_is_never_recorded(guard: ReplayGuard) -> None:
    """The replay guard runs last on purpose. Recording unverified signatures would let
    anyone fill the table by posting garbage, turning a defence into a denial of
    service surface."""
    raw, sent_at, _ = signed(body())
    forged = "v0=" + "1" * 64

    with pytest.raises(BadSignature):
        accept(raw_body=raw, sent_at=sent_at, signature=forged, guard=guard, secret=SECRET)

    assert guard.claim(signature=forged, sent_at=sent_at) is True, (
        "the forged signature had been recorded, so the table is attacker-writable"
    )


def test_deliveries_too_old_to_replay_are_prunable(guard: ReplayGuard) -> None:
    """A row outside the freshness window cannot be replayed - the timestamp check
    refuses it first - so keeping it forever only grows the table."""
    raw, sent_at, signature = signed(body())
    accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)

    assert guard.prune(older_than_seconds=0) == 1
    assert guard.prune(older_than_seconds=0) == 0


# --- verification comes before parsing ---


def test_nothing_is_parsed_before_it_is_verified() -> None:
    """Parsing first would mean acting on the shape of bytes nobody authenticated, and
    every field read out of them would be attacker-controlled input already treated as
    structure."""
    source = inspect.getsource(accept)

    assert source.index("verify(") < source.index("parse_interaction(")


def test_a_malformed_payload_is_a_different_answer_from_a_forged_one() -> None:
    """Authentic-but-unintelligible and forged are different problems with different
    responses; collapsing them loses the distinction in the audit trail."""
    assert not issubclass(MalformedCallback, CallbackRefused)

    with pytest.raises(MalformedCallback, match="no payload"):
        parse_interaction(b"")


def test_an_answer_that_cannot_be_matched_to_a_question_is_refused() -> None:
    with pytest.raises(MalformedCallback, match="does not bind"):
        parse_interaction(body(value="run-7|wait-1"))


def test_an_action_this_system_does_not_offer_is_refused() -> None:
    with pytest.raises(MalformedCallback, match="not an approval action"):
        parse_interaction(body(action="delete_everything"))


def test_an_interaction_with_no_user_is_refused() -> None:
    with pytest.raises(MalformedCallback, match="no user"):
        parse_interaction(body(user=""))


# --- criterion 25's half: the adapter decides nothing ---


def test_the_adapter_has_no_opinion_about_who_may_approve() -> None:
    """The user id is a claim. An adapter that filtered on it would be authorising in
    the place least able to audit it."""
    source = inspect.getsource(slack_callback)

    for forbidden in ("approver_group", "is_authorized", "may_approve", "ALLOWED_USERS"):
        assert forbidden not in source
    assert "claim" in source


def test_the_user_id_is_carried_onward_untouched(guard: ReplayGuard) -> None:
    raw, sent_at, signature = signed(body(user="UNOBODY"))

    reply = accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard, secret=SECRET)

    assert reply.slack_user_id == "UNOBODY", (
        "an unknown user must still arrive; layer 8 refuses them, not this module"
    )


# --- no secret means no acceptance ---


def test_without_a_signing_secret_every_callback_is_refused(
    guard: ReplayGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepting unverified interactions would make the approval boundary decorative."""
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    raw, sent_at, signature = signed(body())

    with pytest.raises(CallbackRefused, match="SLACK_SIGNING_SECRET"):
        accept(raw_body=raw, sent_at=sent_at, signature=signature, guard=guard)


# --- the form decoding, because hand-rolling a slice of stdlib deserves scrutiny ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"payload=hello", "hello"),
        (b"payload=a+b", "a b"),
        (b"payload=%7B%22a%22%3A1%7D", '{"a":1}'),
        (b"token=x&payload=second&other=y", "second"),
        (b"payload=", ""),
        (b"nothing=here", ""),
        (b"payload=caf%C3%A9", "café"),
        (b"payload=100%25", "100%"),
    ],
)
def test_the_form_decoder_matches_what_urllib_would_do(raw: bytes, expected: str) -> None:
    assert slack_callback.form_field(raw, "payload") == expected


def test_the_form_decoder_agrees_with_the_stdlib_on_a_real_payload() -> None:
    """The decoder exists because `urllib` is forbidden to this layer, not because
    stdlib is wrong. Checked against it here, where importing it is allowed."""
    from urllib.parse import parse_qs

    raw = body(user="U+with spaces&ampersand")

    assert slack_callback.form_field(raw, "payload") == parse_qs(raw.decode())["payload"][0]


def test_a_broken_percent_escape_is_refused_rather_than_guessed() -> None:
    with pytest.raises(MalformedCallback, match="percent-escape"):
        slack_callback.form_field(b"payload=%ZZ", "payload")


def test_a_field_name_that_is_a_prefix_of_another_is_not_matched() -> None:
    """`payload_extra=` must not answer a request for `payload`."""
    assert slack_callback.form_field(b"payload_extra=wrong&payload=right", "payload") == "right"
