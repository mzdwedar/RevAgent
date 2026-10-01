# ADR-0003: Separate model asset, serving system and interaction contract

- Status: accepted
- Date: 2026-09-21

## Context

The model-engine layer splits the model engine into three independent choices: the **model asset**
(weights, tokenizer, context window, modality), the **serving system** (queueing,
batching, cache policy, tail latency) and the **interaction contract** (API shape,
tool-call format, state management). Conflating them produces the two most common
foundation errors: treating a long context window as memory, and treating API
compatibility as system equivalence.

## Decision

`agentstack.model` exposes a `ModelEngine` protocol plus a typed `ModelRequest` /
`ModelResponse` contract. The three choices are recorded separately in `SPEC.md`
§ Foundation Assumptions, and a concrete engine is an adapter behind the protocol.

Tool calls returned by an engine are `ToolCallProposal` values. The name is deliberate:
the model proposes; application code decides whether to execute, refuse or validate.
Structured output is treated as untrusted input — schema-valid JSON solves packaging,
not correctness, authorization or policy compliance.

## Consequences

- Swapping a provider is an adapter change, not a runtime change.
- "Our endpoint is OpenAI-compatible" is never accepted as evidence of equivalent
  behavior; benchmarks use real request shapes.
- The context window is a working-set budget. It is recorded as a budget in the spec
  and never used as an argument against owning memory (Context, retrieval, memory).
