# ADR-0004: Egress and filesystem containment land with the first real client

- Status: accepted
- Date: 2026-09-21

## Context

`Sandbox` originally declared `network_allowlist` and `filesystem_roots` alongside the
three dimensions it enforces, and the composition root populated the allowlist. Neither
was ever read by `check()`.

A `/stack-audit` finding put it precisely: a declared-but-unenforced control is worse
than an absent one, because a reviewer sees a configured allowlist and stops looking.
The execution-surfaces layer calls the same thing sandboxing-as-theater.

## Decision

Delete both fields. Containment now declares only what it enforces: surface, resource
prefix, tenant.

`tests/fitness/test_containment.py::test_no_declared_containment_dimension_is_dead`
compares `Sandbox`'s fields against the attributes `check()` actually reads, and fails
if the two ever diverge again. A companion test fails if the class docstring promises
network or filesystem containment that `check()` does not deliver.

## Consequences

- The reference `RecordingClient` has no egress, so nothing is currently unbounded that
  was bounded before. This is a documentation fix, not a capability loss.
- The first real client (HTTP, shell, browser) **must** arrive with its containment
  dimension in the same change: `check()` gains the target host or path, the field
  returns, and the dead-dimension test keeps it honest.
- Recorded here rather than in a comment because the next person to add a client will
  look for a reason the field is missing.
