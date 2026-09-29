"""Settling an effect of unknown outcome, by a person who checked the surface (E2).

The gateway never retries an effect whose answer was lost: it leaves the idempotency
claim unresolved and the run parks on a `reconcile` wait. Only someone who has asked
the surface what happened can say whether it applied. This is where they say it:

1. the claim is settled, as applied (with the surface's receipt) or as not applied
   (the key is released), in one conditional statement so two people cannot both win;
2. an audit record names who settled it, which way, and why;
3. the wait is satisfied, carrying the same.

Waking the run is the caller's (`operator reconcile` signals it through the Temporal
client): the runtime can't reach layer 1, and the record is complete without the
signal. When the run acts again, the ledger answers: a receipt deduplicates, a released
key acts once.

Every step is safe to run again. A person whose command died halfway runs it again and
it finishes; one who runs it twice changes nothing the second time.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentstack.execution.idempotency import ClaimState, IdempotencyLedger
from agentstack.observability.audit import AuditRecord, AuditSink
from agentstack.runtime.run import Run, RunStore
from agentstack.runtime.waits import RECONCILE, ResumeEvent, ResumeRejected, Wait, WaitStore, resume

APPLIED = "reconcile.applied"
NOT_APPLIED = "reconcile.not_applied"


class ReconcileRefused(RuntimeError):
    """The settlement asked for is not one this wait and its claim can take."""


@dataclass(frozen=True, slots=True)
class Settled:
    run: Run
    wait: Wait
    applied: bool
    # False when everything was already done: a second run of the same command.
    changed: bool


def settle(
    *,
    runs: RunStore,
    waits: WaitStore,
    ledger: IdempotencyLedger,
    audit: AuditSink,
    wait_id: str,
    applied: bool,
    receipt: str | None,
    operator: str,
    reason: str,
) -> Settled:
    """Settle the claim a reconcile wait is about, audit who did, and satisfy the wait."""
    if not operator.strip():
        raise ReconcileRefused("a reconciliation names the operator who checked the surface")
    if not reason.strip():
        raise ReconcileRefused("a reconciliation says why: what the surface was found to show")
    if applied and not (receipt or "").strip():
        raise ReconcileRefused("an effect that applied is settled with the surface's receipt")
    if not applied and receipt is not None:
        raise ReconcileRefused("an effect that did not apply has no receipt to record")

    wait = waits.get(wait_id)
    if wait is None or wait.kind != RECONCILE:
        raise ReconcileRefused(f"{wait_id} is not a reconcile wait")
    if wait.idempotency_key is None or wait.action_fingerprint is None:
        raise ReconcileRefused(f"{wait_id} does not name the claim it is about")
    run = runs.get(wait.run_id)
    assert run is not None  # waits.run_id is a foreign key onto runs

    changed = _settle_claim(ledger, wait.idempotency_key, applied=applied, receipt=receipt)
    principal = f"operator:{operator.strip()}"
    if not wait.satisfied:
        # Before the wait is satisfied, so a command that dies between the two leaves the
        # wait pending and is run again, rather than leaving a settlement nobody audited.
        _audit(audit, run, wait, principal=principal, applied=applied)
        try:
            wait = resume(
                waits,
                ResumeEvent(
                    run_id=wait.run_id,
                    wait_id=wait.wait_id,
                    state_snapshot=wait.state_snapshot,
                    payload={
                        "reconciled_by": principal,
                        "applied": applied,
                        "receipt": receipt,
                        "reason": reason,
                    },
                ),
            )
        except ResumeRejected:
            # Satisfied by another run of this command in between: the same answer.
            wait = waits.get(wait_id) or wait
        changed = True
    return Settled(run=run, wait=wait, applied=applied, changed=changed)


def _settle_claim(
    ledger: IdempotencyLedger, key: str, *, applied: bool, receipt: str | None
) -> bool:
    """Settle the claim, or confirm it is already settled the way asked. True if moved."""
    if ledger.reconcile(key, receipt=receipt if applied else None):
        return True
    claim = ledger.inspect(key)
    if applied and claim is not None and claim.state is ClaimState.COMMITTED:
        if claim.receipt != receipt:
            raise ReconcileRefused(
                f"{key} is already settled with receipt {claim.receipt!r}, not {receipt!r}"
            )
        return False
    if not applied and claim is None:
        return False
    found = "released" if claim is None else f"settled with {claim.receipt!r}"
    raise ReconcileRefused(
        f"{key} is already {found}; it cannot also be settled as "
        f"{'applied' if applied else 'not applied'}"
    )


def _audit(audit: AuditSink, run: Run, wait: Wait, *, principal: str, applied: bool) -> AuditRecord:
    """Who settled it, which way. The surface and resource are the act's own, taken from
    the gateway's record of the attempt that came back unknown."""
    attempt = next(
        (
            r
            for r in reversed(audit.for_run(run.run_id))
            if r.action_fingerprint == wait.action_fingerprint and r.outcome == "unresolved"
        ),
        None,
    )
    assert wait.action_fingerprint is not None and wait.idempotency_key is not None
    return audit.write(
        run_id=run.run_id,
        principal=principal,
        tenant=run.tenant,
        action_fingerprint=wait.action_fingerprint,
        surface="unknown" if attempt is None else attempt.surface,
        resource=wait.idempotency_key if attempt is None else attempt.resource,
        policy_decision=APPLIED if applied else NOT_APPLIED,
        approval_id=None,
        outcome="reconciled",
    )
