"""Asking a human, in Slack (Interfaces & channels, and the Execution surfaces choke point made
visible).

This is a channel, not an execution surface. It carries a question out and, in T15, a
claim back; it changes no business state, and routing the approval request through the
approval gateway would be circular. Like the model serving system, it uses its own SDK
rather than an HTTP client of its own.

**Criterion 15 is structural here, not a formatting convention.** An `ApprovalAsk`
cannot be built without an estimated headcount. "10% of the targeted cohort" is not a
quantity a person can weigh - it is a ratio whose denominator the reader does not know -
and a prompt that shows only a percentage is one a tired approver will wave through.
Making the number a required field means a later edit cannot quietly drop it.

**A failed notification is loud.** If the message never arrives, nobody is asked, and
the run waits for an answer to a question that was never put. That state is
indistinguishable from "waiting patiently" unless the failure says so, which is why
`post_approval` raises rather than returning a falsy value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

TOKEN_VARIABLE = "SLACK_BOT_TOKEN"

# Slack truncates hard. The summary is the part a person actually reads, so it is what
# gets the room rather than the metadata around it.
MAX_SUMMARY = 2_800


class NotificationFailed(RuntimeError):
    """The question did not reach anyone. Nobody is waiting on the other end."""


class ApprovalNotSpecific(ValueError):
    """The ask does not carry a quantity a person can weigh."""


def customers_affected(cohort_size: int, percentage: int) -> int:
    """How many real people a rollout reaches, from the frozen cohort and the share."""
    if cohort_size < 0 or not 0 <= percentage <= 100:
        raise ApprovalNotSpecific(
            f"cannot estimate a headcount from cohort={cohort_size}, percentage={percentage}"
        )
    return round(cohort_size * percentage / 100)


@dataclass(frozen=True, slots=True)
class ApprovalAsk:
    """One question put to a person, and everything needed to bind their answer.

    `run_id`, `wait_id`, `experiment_version` and `data_as_of` travel with the message
    because the answer has to come back bound to the exact action, the exact frozen
    cohort and the exact wait. An approval that arrives knowing only "yes" cannot be
    matched to what was asked.
    """

    run_id: str
    wait_id: str
    summary: str
    experiment_version: str
    data_as_of: str
    percentage: int
    estimated_customers: int
    annual_value_at_risk_cents: int
    tenant: str

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ApprovalNotSpecific("an approval ask with no summary asks nothing")
        if self.estimated_customers < 1:
            raise ApprovalNotSpecific(
                f"this rollout reaches {self.estimated_customers} customers; there is "
                "nothing to approve, and waking someone for it teaches them the asks "
                "do not matter"
            )
        if not self.experiment_version or not self.data_as_of:
            raise ApprovalNotSpecific(
                "an approval must name the frozen cohort it is granted against, or it "
                "cannot be refused later for being stale"
            )

    def headline(self) -> str:
        """The line that has to work on a phone notification, on its own."""
        return (
            f"Roll out to ~{self.estimated_customers:,} customers "
            f"({self.percentage}% of the targeted cohort)"
        )

    def money(self) -> str:
        return f"${self.annual_value_at_risk_cents / 100:,.0f}"


def render_blocks(ask: ApprovalAsk) -> list[dict[str, Any]]:
    """Slack Block Kit for one approval.

    The headcount is in the headline rather than in a field below it, because the
    headline is what a phone notification shows and what a person decides from.
    """
    return [
        {"type": "header", "text": {"type": "plain_text", "text": ask.headline()}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```{ask.summary[:MAX_SUMMARY]}```"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Customers affected*\n~{ask.estimated_customers:,}"},
                {"type": "mrkdwn", "text": f"*Annual revenue at risk*\n{ask.money()}"},
                {"type": "mrkdwn", "text": f"*Experiment*\n`{ask.experiment_version}`"},
                {"type": "mrkdwn", "text": f"*Data as of*\n`{ask.data_as_of}`"},
            ],
        },
        {
            "type": "actions",
            # The ids travel in the button value, so the answer comes back bound to the
            # run, the wait and the frozen cohort rather than to whoever clicked.
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "approve_rollout",
                    "value": _binding(ask),
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Refuse"},
                    "action_id": "refuse_rollout",
                    "value": _binding(ask),
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"run `{ask.run_id}` · wait `{ask.wait_id}` · tenant "
                        f"`{ask.tenant}` · this approval is granted against this "
                        "cohort and expires if the data moves"
                    ),
                }
            ],
        },
    ]


def answered_text(*, approved: bool, approver: str) -> str:
    """The line an answered question ends on, and its plain-text fallback."""
    return f"{'Approved' if approved else 'Refused'} by {approver}"


def render_answered_blocks(
    original: list[dict[str, Any]], *, approved: bool, approver: str
) -> list[dict[str, Any]]:
    """The same message with its buttons gone and the answer in their place.

    The buttons are what make a message look open. Once the answer is recorded a second
    click could only be refused as a replay, so leaving them up invites it without
    telling the person that it was already dealt with. The wording comes from the
    recorded answer, never from the click's payload.
    """
    kept = [block for block in original if block.get("type") != "actions"]
    icon = ":white_check_mark:" if approved else ":no_entry_sign:"
    kept.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{icon} *{answered_text(approved=approved, approver=approver)}*",
            },
        }
    )
    return kept


def _binding(ask: ApprovalAsk) -> str:
    return f"{ask.run_id}|{ask.wait_id}|{ask.experiment_version}|{ask.data_as_of}"


def _message_id(answer: Any) -> str:
    """Slack's own client returns a mapping-like response; a fake returns a dict."""
    if isinstance(answer, dict):
        return str(answer.get("ts") or "")
    getter = getattr(answer, "get", None)
    return str(getter("ts") or "") if callable(getter) else ""


class Notifier(Protocol):
    """Where an approval question goes. Returns the message's own id."""

    def post_approval(self, ask: ApprovalAsk, *, channel: str) -> str: ...


@dataclass(frozen=True, slots=True)
class SlackNotifier:
    """The real one. Needs `SLACK_BOT_TOKEN`; the credential store is iteration-2 debt."""

    default_channel: str = "#experiments"
    build_client: Any = None

    def _client(self) -> Any:
        if self.build_client is not None:
            return self.build_client()
        token = os.environ.get(TOKEN_VARIABLE, "").strip()
        if not token:
            raise NotificationFailed(
                f"{TOKEN_VARIABLE} is not set, so no approval can be requested. A run "
                "that parks on a wait nobody was asked about waits forever."
            )
        from slack_sdk import WebClient

        return WebClient(token=token)

    def post_approval(self, ask: ApprovalAsk, *, channel: str = "") -> str:
        blocks = render_blocks(ask)
        try:
            answer = self._client().chat_postMessage(
                channel=channel or self.default_channel,
                blocks=blocks,
                # Shown in notifications and by clients that cannot render blocks. It
                # carries the headcount too, so the quantity survives the fallback.
                text=ask.headline(),
            )
        except NotificationFailed:
            raise
        except Exception as exc:
            raise NotificationFailed(
                f"the approval for run {ask.run_id} did not reach "
                f"{channel or self.default_channel}: {exc}"
            ) from exc
        timestamp = _message_id(answer)
        if not timestamp:
            raise NotificationFailed(
                f"Slack accepted the approval for run {ask.run_id} without returning a "
                "message id; there is no way to tell whether anyone can see it"
            )
        return timestamp


@dataclass(slots=True)
class RecordingNotifier:
    """Keeps the asks instead of posting them, for tests and for dev without a workspace."""

    posted: list[tuple[str, ApprovalAsk]] = field(default_factory=list)
    fail: bool = False

    def post_approval(self, ask: ApprovalAsk, *, channel: str = "#experiments") -> str:
        if self.fail:
            raise NotificationFailed(f"could not reach {channel}")
        self.posted.append((channel, ask))
        return f"ts-{len(self.posted)}"
