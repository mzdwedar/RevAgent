#!/usr/bin/env bash
# The edit-loop gate. Exit 2 hands the failure back to the model as feedback, which
# is the whole point: the agent fixes its own output instead of a human finding it.
set -uo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$ROOT" || exit 0

payload=$(cat)
file=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // ""' 2>/dev/null)

# Only gate on files the checks actually cover.
case "$file" in
  *.py|*.toml|*.importlinter|*CONSTRAINTS.md|*STACK.md|"") ;;
  *) exit 0 ;;
esac

if out=$(bash scripts/check_fast.sh 2>&1); then
  exit 0
fi

{
  echo "check_fast.sh failed after editing ${file:-the working tree}."
  echo "$out" | grep -v '^ok    ' | head -40
  echo
  echo "Fix the code. Do not silence the check, lower a threshold, or edit"
  echo "CONSTRAINTS.md / STACK.md / .importlinter to make this pass."
} >&2
exit 2
