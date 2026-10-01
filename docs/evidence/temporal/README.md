# Temporal probe results

Measured output of the probes that justified Temporal as the durable-execution backend
and the idempotency / approval-at-the-act / gateway-only enforcement (ADR-0007,
ADR-0008). The probe code was deleted; it is recoverable at commit `c48553c` under
`spikes/temporal/`. It ran against the tree as it was then, and stopped running once
ADR-0010 removed the billing catalog. `RESULTS.txt` and `RESULTS-enforcement.txt` are
the record.
