"""Layer 10 - the state substrate the agent runs on.

Every other layer's store lands here: sessions and transcripts (layer 2), steps and
waits (layer 3), approvals, the idempotency ledger and audit records (layers 7-9).

This is the *only* package that imports the database driver, and it is deliberately
not `agentstack.execution`. Layer 7 exists to gate the systems the agent acts *upon* -
policy, approval, containment, an identity envelope per call. The agent's own state is
not one of those: recording that a step completed is not an effect anyone approves, and
`agentstack.context` cannot reach layer 7 at all (contract 2). Collapsing the two would
make "surface" mean two different things.

`lint-imports` contract 3 forbids `psycopg` everywhere else. See docs/adr/0005.
"""
