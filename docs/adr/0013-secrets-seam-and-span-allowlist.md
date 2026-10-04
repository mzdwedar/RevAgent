# ADR-0013: One door for secrets, an allowlist for spans

- Status: **accepted** (the seam and the allowlist); retention and the secret store are
  **not decided** and are named below.
- Date: 2026-10-04
- Layers: 9 (observability), 10 (infrastructure substrate), 1 and 4b (their readers)

## Context

Two debts were named in `SPEC.md` and carried through iteration 1: personal data in the
trace store, and secrets that live in environment variables. Both were "iteration 2".
Neither can be closed by a test alone, but two halves of each can be made enforceable now.

## Decision

1. **Spans export an allowlist** (`observability/redaction.py`). A span attribute leaves the
   process only if its name is listed. Anything else is dropped and only its *name* is
   reported (`dropped_attributes`). Free text (`reason`) is also truncated and has
   addresses, long numbers, token shapes and any revealed secret masked. The allowlist is
   the guarantee; the text rules are a net. `LoggingSink` applies it, and
   `test_span_attributes.py` holds every `tracer.span(...)` call site to the list, and
   holds the list to what is used.
2. **Secrets are read through `storage/secrets.py`.** A `Secret` prints its name and never
   its value, cannot be pickled, and registers its value when revealed so `mask` can find
   it again. `DATABASE_URL`, `TABPFN_TOKEN`, the Slack bot token and the Slack signing
   secret all enter here. `test_secrets.py` holds the set of modules that read the
   environment to three named ones, none of which reads a credential.

The seam lives in layer 10 because every layer already imports it and a new package would
need a new ledger row; the substrate is where a process's own configuration belongs.

## What this does not do

- **The source is still the process environment.** Environment variables are not a secret
  store, and this does not make them one. It makes moving to one a change to a single
  file. The choice of store (cloud secret manager, Vault, cluster secrets) is a deployment
  decision and is **open**, as is rotation, including rotating the Slack signing secret
  without stranding approvals already waiting.
- **Retention is not decided.** How long traces, transcripts and audit rows live, and the
  job that enforces it, are the owner's call. Until then nothing is deleted.
- **Redaction of prose is a net.** A person or a model can phrase personal data in a way no
  pattern catches. The allowlist keeps customer rows out of spans; it cannot vouch for what
  a `reason` says.
- **Transcripts and the audit sink are untouched.** Audit rows hold approver identities on
  purpose: accountability needs them.
- **`.env` loading is unchanged**, and `scripts/` still read the environment directly.

## Consequences

- Adding a span attribute is an edit to `redaction.py` that a reviewer sees.
- A secret interpolated into a message, a log line or a span is masked once it has been
  revealed, and a `Secret` cannot be printed by accident.
- `STACK.md` was not edited: its layer-10 row does not mention secrets, and that file is
  hook-protected. The owner may want a sentence there.
