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
)

# Raised by `interceptors.DeclaredActivitiesOnly`, before the activity body runs.
UNDECLARED = "UndeclaredActivity"

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    non_retryable_error_types=[*REFUSALS, UNDECLARED],
)
