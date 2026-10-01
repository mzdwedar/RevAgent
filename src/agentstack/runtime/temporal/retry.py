"""The one RetryPolicy (ADR-0008 rule 2).

Temporal's default retries a failing activity forever. For a transient failure (a
dropped connection, a restarted database) that's right. For a refusal it's wrong
twice over. A refusal is an answer, and asking again gets the same answer. And
`UnresolvedEffect` means an effect may already have landed, so a blind retry is how one
rollout becomes two (E2). Each of these fails the activity on its first attempt, and
the workflow decides what the refusal means.

Names, not classes. Workflow code uses this policy, and contract 6 forbids workflow
code any path to `execution` or `policy`. `test_temporal_boundaries` resolves every
name here to the real exception class, so a rename can't quietly turn a refusal back
into something that gets retried.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

# Refused by the gateway or by layer 8. Retrying any of these can only repeat the answer,
# or, for UnresolvedEffect, repeat the effect.
REFUSALS = (
    "UnresolvedEffect",
    "SurfaceRefused",
    "SandboxViolation",
    "PolicyDenied",
    "ApprovalRequired",
    "ApprovalStale",
    "ApproverNotAuthorized",
    # A trigger kind reaching an outcome it may not (policy/triggers.py). Retrying
    # it is worse than pointless: the second attempt finds the cycle already claimed,
    # returns it unsettled, and the refusal disappears into a success.
    "OutcomeNotAuthorized",
    # A trigger naming a tenant other than its run's (policy/triggers.py). The same
    # state refuses it every time.
    "TenantClaimRefused",
)

# Not refusals: failures that the same state reproduces every time. A store that expects a
# conflict catches `IntegrityViolation` itself (steps.py, surfaces.py), and every activity
# write is an upsert or a claim, so one that reaches the activity boundary is a bug. Retrying
# it only hides it: T34's missing session spun silently at one attempt a minute.
# Postgres's transient failures (deadlock, serialization, a dropped connection) are other
# classes, and they still retry.
DETERMINISTIC = ("IntegrityViolation",)

# Raised by `interceptors.DeclaredActivitiesOnly`, before the activity body runs.
UNDECLARED = "UndeclaredActivity"

# Past this many attempts, `operator status` flags a pending activity with its last
# failure. Not a cap: the activity keeps retrying. It's the point a person should look.
STUCK_AFTER_ATTEMPTS = 10

# No attempt cap, deliberately. A cap can't tell an outage from a bug, and a run parked for
# weeks on an approval must not die because Postgres was down for twenty minutes. What's
# still retried indefinitely is surfaced by `operator status` instead (T50).
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    non_retryable_error_types=[*REFUSALS, *DETERMINISTIC, UNDECLARED],
)
