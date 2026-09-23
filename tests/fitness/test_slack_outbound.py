"""Criterion 15: the approver sees a headcount.

"10% of the targeted cohort" is a ratio whose denominator the reader does not know.
"~33 customers" is a quantity a person can weigh. A prompt that shows only the
percentage is one a tired approver waves through, and that is the failure the whole
approval boundary exists to prevent - a modal that trains people to click yes.

So the headcount is a required field on `ApprovalAsk`, not a formatting convention.
A later edit cannot quietly drop it.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from agentstack.interfaces.slack import (
    ApprovalAsk,
    ApprovalNotSpecific,
    NotificationFailed,
    RecordingNotifier,
    SlackNotifier,
    customers_affected,
    render_blocks,
)

ASK = {
    "run_id": "run-7",
    "wait_id": "wait-1",
    "summary": "roll_out_variant_to_percentage — IRREVERSIBLE\nresource: acme/experiments/exp-7",
    "experiment_version": "exp:5cbf2762f2709789",
    "data_as_of": "telecom-bigml:f107d488f7bf4651",
    "percentage": 10,
    "estimated_customers": 33,
    "annual_value_at_risk_cents": 24_212_000,
    "tenant": "acme",
}


def ask(**overrides: Any) -> ApprovalAsk:
    return ApprovalAsk(**{**ASK, **overrides})


# --- criterion 15 ---


def test_the_headline_names_a_number_of_customers() -> None:
    """The headline is what a phone notification shows, and what someone decides from
    without opening the app."""
    headline = ask().headline()

    assert "33 customers" in headline
    assert "10%" in headline, "the share is useful context, just never the only quantity"


def test_a_percentage_alone_is_not_enough_to_ask_anyone() -> None:
    """The structural half: the headcount is a required field with no default, so an
    ask cannot be constructed without one and a later edit cannot quietly drop it."""
    field = ApprovalAsk.__dataclass_fields__["estimated_customers"]

    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING


def test_a_rollout_that_reaches_nobody_is_not_worth_waking_someone_for() -> None:
    """Asking about nothing teaches people the asks do not matter."""
    with pytest.raises(ApprovalNotSpecific, match="nothing to approve"):
        ask(estimated_customers=0)


def test_the_headcount_survives_the_notification_fallback() -> None:
    """Clients that cannot render blocks show the fallback text. The quantity has to be
    in it, or the fallback is the percentage-only prompt this criterion forbids."""
    notifier = RecordingNotifier()
    sent: dict[str, Any] = {}

    class Capturing:
        def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            sent.update(kwargs)
            return {"ts": "1700000000.000100"}

    SlackNotifier(build_client=lambda: Capturing()).post_approval(ask(), channel="#x")

    assert "33 customers" in sent["text"]
    assert notifier.posted == []


def test_the_money_is_shown_in_money() -> None:
    assert ask().money() == "$242,120"


@pytest.mark.parametrize(
    ("size", "percentage", "expected"),
    [(334, 10, 33), (1000, 10, 100), (334, 100, 334), (334, 0, 0), (0, 10, 0)],
)
def test_the_headcount_is_derived_from_the_frozen_cohort(
    size: int, percentage: int, expected: int
) -> None:
    assert customers_affected(size, percentage) == expected


@pytest.mark.parametrize(("size", "percentage"), [(-1, 10), (100, 101), (100, -1)])
def test_a_headcount_that_cannot_be_estimated_is_refused(size: int, percentage: int) -> None:
    with pytest.raises(ApprovalNotSpecific):
        customers_affected(size, percentage)


# --- the answer has to come back bound to what was asked ---


def test_the_buttons_carry_the_run_the_wait_and_the_frozen_cohort() -> None:
    """An approval arriving knowing only "yes" cannot be matched to what was asked."""
    actions = next(b for b in render_blocks(ask()) if b["type"] == "actions")

    for button in actions["elements"]:
        assert button["value"] == "run-7|wait-1|exp:5cbf2762f2709789|telecom-bigml:f107d488f7bf4651"


def test_both_answers_are_offered() -> None:
    """A prompt with only an approve button is a prompt with one answer."""
    actions = next(b for b in render_blocks(ask()) if b["type"] == "actions")
    ids = {button["action_id"] for button in actions["elements"]}

    assert ids == {"approve_rollout", "refuse_rollout"}


def test_an_ask_without_a_frozen_cohort_is_refused() -> None:
    """It could never be refused later for being stale, because nothing says what it
    was granted against."""
    with pytest.raises(ApprovalNotSpecific, match="frozen cohort"):
        ask(experiment_version="")


def test_an_empty_summary_asks_nothing() -> None:
    with pytest.raises(ApprovalNotSpecific, match="asks nothing"):
        ask(summary="   ")


# --- the blocks are what Slack will accept ---


def test_the_blocks_are_serialisable_and_well_formed() -> None:
    blocks = render_blocks(ask())

    json.dumps(blocks)
    assert [b["type"] for b in blocks] == ["header", "section", "section", "actions", "context"]


def test_the_summary_is_truncated_rather_than_rejected_by_slack() -> None:
    """Slack truncates hard. Losing the end of a long prompt is better than the post
    failing and nobody being asked at all."""
    blocks = render_blocks(ask(summary="x" * 5_000))
    rendered = blocks[1]["text"]["text"]

    assert len(rendered) < 3_000


def test_the_fields_name_the_data_the_approval_is_bound_to() -> None:
    fields = next(b for b in render_blocks(ask()) if b.get("fields"))
    text = " ".join(f["text"] for f in fields["fields"])

    assert "exp:5cbf2762f2709789" in text
    assert "telecom-bigml:f107d488f7bf4651" in text
    assert "$242,120" in text


# --- a question nobody received is not a question ---


def test_a_failed_post_is_loud() -> None:
    """A run parked on a wait nobody was asked about waits forever, and that state is
    indistinguishable from waiting patiently unless the failure says so."""
    notifier = RecordingNotifier(fail=True)

    with pytest.raises(NotificationFailed, match="#experiments"):
        notifier.post_approval(ask(), channel="#experiments")


def test_a_missing_token_is_reported_as_nobody_being_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The variable is cleared here rather than assumed absent: a test that passes
    because of the developer's shell fails on the machine that has a token."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    with pytest.raises(NotificationFailed, match="waits forever"):
        SlackNotifier().post_approval(ask(), channel="#x")


def test_slack_accepting_without_a_message_id_is_a_failure() -> None:
    """Accepted-but-invisible is worse than refused: the run would wait on a message
    nobody can see and nothing would say so."""

    class Silent:
        def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["blocks"], "even a silent Slack is sent the blocks"
            return {"ok": True}

    with pytest.raises(NotificationFailed, match="without returning a message id"):
        SlackNotifier(build_client=lambda: Silent()).post_approval(ask(), channel="#x")


def test_a_posted_ask_returns_the_message_id_to_bind_a_reply_to() -> None:
    class Accepting:
        def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["channel"] == "#x"
            return {"ts": "1700000000.000100"}

    timestamp = SlackNotifier(build_client=lambda: Accepting()).post_approval(ask(), channel="#x")

    assert timestamp == "1700000000.000100"


def test_a_slack_error_names_the_channel_nobody_was_asked_in() -> None:
    """An operator reading this needs to know where the question failed to land."""

    class Refusing:
        def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(f"channel_not_found: {kwargs['channel']}")

    notifier = SlackNotifier(build_client=lambda: Refusing())

    with pytest.raises(NotificationFailed) as caught:
        notifier.post_approval(ask(), channel="#gone")

    assert "#gone" in str(caught.value)
    assert "run-7" in str(caught.value)


def test_slack_s_own_response_object_is_read_like_a_mapping() -> None:
    """The real client returns a SlackResponse, not a dict. A fake that only ever
    returned dicts would leave this path unexercised until production."""

    class SlackResponse:
        def __init__(self, data: dict[str, Any]) -> None:
            self.data = data

        def get(self, key: str) -> Any:
            return self.data.get(key)

    class Accepting:
        def chat_postMessage(self, **kwargs: Any) -> Any:
            assert kwargs["text"]
            return SlackResponse({"ts": "1700000000.000200"})

    timestamp = SlackNotifier(build_client=lambda: Accepting()).post_approval(ask(), channel="#x")

    assert timestamp == "1700000000.000200"


def test_a_response_that_is_neither_is_treated_as_nobody_being_asked() -> None:
    """Accepted-but-unreadable is the same problem as accepted-but-invisible."""

    class Odd:
        def chat_postMessage(self, **kwargs: Any) -> Any:
            assert kwargs["blocks"]
            return object()

    with pytest.raises(NotificationFailed, match="without returning a message id"):
        SlackNotifier(build_client=lambda: Odd()).post_approval(ask(), channel="#x")


def test_the_default_channel_is_used_when_none_is_given() -> None:
    sent: dict[str, Any] = {}

    class Capturing:
        def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            sent.update(kwargs)
            return {"ts": "1"}

    SlackNotifier(default_channel="#retention", build_client=lambda: Capturing()).post_approval(
        ask()
    )

    assert sent["channel"] == "#retention"


def test_the_recorder_keeps_what_was_asked_and_where() -> None:
    """The dev and test path: no workspace, and still an answerable question about what
    would have been sent."""
    notifier = RecordingNotifier()

    first = notifier.post_approval(ask(), channel="#experiments")
    second = notifier.post_approval(ask(percentage=25, estimated_customers=84), channel="#other")

    assert [channel for channel, _ in notifier.posted] == ["#experiments", "#other"]
    assert notifier.posted[1][1].estimated_customers == 84
    assert first != second, "each ask needs its own message id to bind a reply to"
