#!/usr/bin/env bash
# The turn-end gate: a turn does not end below the bar.
set -uo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$ROOT" || exit 0

payload=$(cat)
# Never re-block a turn this hook already blocked once, or the session loops.
if [ "$(printf '%s' "$payload" | jq -r '.stop_hook_active // false' 2>/dev/null)" = "true" ]; then
  exit 0
fi

# Nothing changed, nothing to check.
if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
  exit 0
fi

if out=$(bash scripts/check_task.sh 2>&1); then
  exit 0
fi

{
  echo "check_task.sh failed, so this turn is not done."
  echo "$out" | grep -vE '^ok    ' | tail -60
} >&2
exit 2
