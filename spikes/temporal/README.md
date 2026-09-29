# Point-in-time evidence, not runnable

These probes justified Temporal as the durable-execution backend and the idempotency / approval-at-the-act / gateway-only enforcement (see ADR-0007, ADR-0008). They ran against the tree as it was then;
since ADR-0010 removed the billing catalog, the ones that call `issue_refund` /
`billing:*` no longer run. Nothing imports this directory and no gate runs it. The
`RESULTS*.txt` files are the record of what was measured.
