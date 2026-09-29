#!/usr/bin/env bash
# Shared by the hooks. Source it; it defines helpers and never exits.

# The Haiku triage call runs `claude -p`, which would fire these very hooks. It sets
# HARNESS_TRIAGE=1 so they stand down instead of recursing.
harness_nested() { [ -n "${HARNESS_TRIAGE:-}" ]; }

# One JSON line per hook run, to measure where the tokens go before and after a change.
# hlog <hook> <exit> <output_bytes> <start_epoch_seconds>
hlog() {
  mkdir -p "$ROOT/.tmp" 2>/dev/null || return 0
  printf '{"ts":%s,"hook":"%s","exit":%s,"output_bytes":%s,"seconds":%s}\n' \
    "$(date +%s)" "$1" "$2" "$3" "$(( $(date +%s) - $4 ))" >>"$ROOT/.tmp/harness_cost.log" 2>/dev/null
  return 0
}
