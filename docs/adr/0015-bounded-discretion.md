# ADR-0015: Bounded discretion: the model chooses the offer and the percentage, policy holds the bounds

- Status: **accepted** (the split and the bounds); built by D1–D5 of `tasks/todo-agent-quality.md`.
- Date: 2026-10-05
- Layers: 8 (policy: the bounds), 6 (tools: the arguments), 3 (runtime: the draft and rollout
  turns), 5 (context: what the model may *not* use to move a bound)

## Context

Today the model writes two free-text strings (`hypothesis`, `variant`) and is dictated every
rollout argument ("exactly as given"); `rollout_deviation` refuses any difference from the
record. That makes the agent safe and also makes its decisions unmeasurable: nothing
structured says what an offer costs, and a model that only copies cannot be better or worse
than another. `SPEC.md` lists "widening the rollout percentage beyond what was specced" as
ask-first. The owner asked for it on 2026-10-05 and signed off the bounds below.

## Decision

1. **Two kinds of argument.**
   - *Record-held*: experiment id, `experiment_version`, resource, cohort predicate. Exact
     match against the record, as today. The model never chooses them.
   - *Discretionary*: `offer_kind`, `offer_amount`, `offer_months` (draft) and `percentage`
     (rollout). The model proposes; admission checks them against bounds.
2. **The bounds live in `experiments/offers.toml`, read by `policy/offers.py` (layer 8).**
   They are versioned with the experiment like `targeting.toml`. A proposal outside them is
   refused before it reaches a person and is audited (`proposal.out_of_bounds`), with the
   reason string naming the bound.
3. **Bounds never come from context.** No retrieved text, memory item, hypothesis, tool
   result or prompt can raise, lower or add a bound. `Bounds` is loaded from config only, and
   the loader takes no context argument. The model is *told* the bounds so it can propose
   inside them; telling is not granting.
4. **The approval binds the exact values chosen.** The prompt shows each chosen value with
   its allowed range and the headcount, and the fingerprint the wait recorded covers them.
   Approved equals executed.
5. **The bounds, as signed off:**
   - **Rollout percentage 5–15%**, and the treatment arm must be **at least 100 customers**
     (the smallest measurable arm, per `targeting.toml`). On a 1,000-customer cohort that
     forces at least 10%. The alternative, 5–10%, was declined.
   - **Offer catalog:** `percent_off` 10–50% for 1–3 months; `free_months` 1–2;
     `tier_upgrade` 1–3 months at the higher tier.
   - **Cost cap:** total offer cost over the treatment arm is at most **5% of the cohort's
     annual value at risk**. Cost per treated customer comes from **observed ARPU** in the
     cohort's currency. An unknown kind or a missing currency is refused, never defaulted.
6. **The draft menu stays 5, and the model may not decline to draft.** A turn with no
   proposal remains a recorded non-event, which E3 counts. Declining would change a menu
   `CONSTRAINTS.md` pins, and needs its own ADR.

## What this does not do

- It does not let the model choose the cohort, the metric, the thresholds in
  `targeting.toml`, or the approval tier.
- It does not make the offer *good*. Bounds limit harm; quality is what Part E measures.
- The numbers are a first position, not a finding. Changing one is a line in
  `offers.toml` and, being a policy threshold, is ask-first.
- The 5–15% range supersedes "~10%" in `SPEC.md`. It does not relax the 100-customer floor.

## Consequences

- `SPEC.md` iteration-1 decision 1 now says the percentage is chosen within the range.
- `CONSTRAINTS.md` gains three rows and loses none. Their tests land with D1–D3; until a row's
  test exists it says so, rather than naming a command that passes vacuously.
- The "Registry narrowness" row (only the rollout names a `percentage`; no array or object
  argument; menus 5 / 5 / 4) is unchanged. The offer is three flat scalars on the existing
  draft tool.
