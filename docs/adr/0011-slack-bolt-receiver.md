# ADR-0011: Receive Slack's approval answers with Slack Bolt (HTTP mode)

- Status: **accepted**
- Date: 2026-09-29
- Layers: 1 (interfaces), 8 (policy, unchanged), 3 (runtime, unchanged)

## Context

An approval is posted to Slack by `SlackNotifier` and answered by a button click.
`wiring.answer()` already does everything after the click: `slack_callback.accept`
proves the request is Slack's, fresh and first of its kind, `ApprovalCoordinator.apply`
decides whether that person may answer for the run's tenant, the answer is recorded, and
the run is woken. Nothing serves that path over HTTP, so a real approval cannot be
answered end to end.

## Decision

Add `agentstack.interfaces.slack_app`: a Slack Bolt (`slack-bolt`) app in **HTTP mode**
with one action handler. The handler takes the raw request body, the
`X-Slack-Request-Timestamp` and the `X-Slack-Signature` headers and passes them,
unchanged, to `wiring.answer()`. It contains no policy and no parsing of its own.

- **Our verification stays the authority.** `slack_callback.verify` (signature over the
  raw bytes, timestamp window, replay claim in Postgres) runs before anything reads the
  payload. Bolt's own signature check is left enabled as a second, redundant gate; it is
  never the only one, and turning it off does not weaken the bar.
- **Socket Mode is rejected.** It authenticates the websocket, not the request, so there
  is no signature or timestamp to verify and the replay guard has nothing to claim.
  Accepting it would make `accept()` unreachable for the one channel that matters, which
  is a collapse of "is this really from Slack" into transport trust.
- **Bolt is an adapter, not a dependency of the stack.** Only `agentstack.interfaces`
  imports it (`.importlinter` contract 4 already keeps the rest of the stack from
  importing `interfaces`). No layer imports `slack_bolt`.
- **The listener does not decide who may approve.** It never reads a user id as an
  authority claim; `slack_user_id` remains a claim until layer 8 checks it against
  `approvers` (see `policy/approvers.py`).

Adds one dependency, `slack-bolt`, approved by the maintainer before this ADR.

## Consequences

- A public URL is required to receive callbacks (a tunnel in dev). The URL is set once as
  the app's Interactivity Request URL.
- The receiver is a second long-running process beside `agentstack-worker`. It holds no
  run state: it writes the answer through layer 8 and signals Temporal, so it can restart
  at any time without stranding a run.
- A failed verification or an unknown approver returns a refusal to Slack and never
  reaches the run; the refusal is audited by layer 8 as it is today.

## Verification

- `tests/fitness/test_slack_inbound.py` continues to prove bad signature, stale
  timestamp and replay are each refused before any policy runs; a new test drives the
  Bolt handler with those three cases and asserts the run is not woken.
- An import-linter check that `slack_bolt` is imported only under `agentstack.interfaces`.

## Addendum: the answered message is redrawn

After `answer` returns, the listener replaces the clicked message's buttons with the
recorded answer ("Approved by …" / "Refused by …") through `chat_update`. It is
`chat_update` and not the click's `response_url` because that URL expires after thirty
minutes and an approval can wait longer. The click's payload only chooses which message
to redraw (channel, `ts`, its blocks); the words come from the `Resolution`, so
untrusted text never becomes the record of who decided. The update happens only after a
recorded answer, and its failure is logged rather than raised: the answer stands, and a
5xx would only make Slack retry a click the replay guard refuses. A refused click leaves
the message as it was. No policy moves into the listener.
