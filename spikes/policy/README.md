# Point-in-time evidence, not runnable

These probes justified keeping the authority decision in Python rather than Cedar or Rego (see ADR-0008). They ran against the tree as it was then;
since ADR-0010 removed the billing catalog, the ones that call `issue_refund` /
`billing:*` no longer run. Nothing imports this directory and no gate runs it. The
`RESULTS*.txt` files are the record of what was measured.
