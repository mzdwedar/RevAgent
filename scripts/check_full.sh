#!/usr/bin/env bash
# The CI gate. Minutes are fine here; this is where the external opinions run.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
# Includes both deploy guards, checkpoint_guard and replay_guard: cheap enough for turn end.
bash scripts/check_task.sh || fail=1

echo "----- release gates (Part 8)"
uv run python -m evals run --gates || fail=1

echo "----- dependencies"
if command -v osv-scanner >/dev/null 2>&1; then
  osv-scanner scan source -r . || fail=1
else
  echo "skip  osv-scanner not installed"
fi

echo "----- secrets"
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --redact --no-banner || fail=1
else
  echo "skip  gitleaks not installed"
fi

exit $fail
