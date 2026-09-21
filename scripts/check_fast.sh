#!/usr/bin/env bash
# The edit-loop gate. Budget: under 5 seconds.
#
# Only things that are cheap and absolute live here: lint, format, types, layer
# contracts, and the two fitness tests that catch a collapsed boundary the moment it
# is written. Anything slower belongs in check_task.sh - that is a placement decision,
# not a reason to drop the check.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
run() {
  local label="$1"; shift
  local out
  if ! out=$("$@" 2>&1); then
    echo "FAIL  $label"
    echo "$out" | tail -25
    fail=1
  else
    echo "ok    $label"
  fi
}

run "ruff"            uv run ruff check .
run "format"          uv run ruff format --check .
run "types"           uv run mypy
run "layers"          uv run lint-imports
run "capabilities"    uv run pytest -q tests/fitness/test_tool_registry.py \
                                      tests/fitness/test_layer_boundaries.py \
                                      tests/fitness/test_capability_is_not_execution.py

if command -v gitleaks >/dev/null 2>&1; then
  run "secrets"       gitleaks detect --redact --no-banner
else
  echo "skip  secrets (gitleaks not installed; CI enforces it)"
fi

exit $fail
