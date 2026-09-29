#!/usr/bin/env bash
# The edit-loop gate. Exit 2 hands the failure back to the model as feedback, which
# is the whole point: the agent fixes its own output instead of a human finding it.
set -uo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
# shellcheck source=_lib.sh
. "$ROOT/.claude/hooks/_lib.sh"
harness_nested && exit 0
cd "$ROOT" || exit 0
started=$(date +%s)

payload=$(cat)
file=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // ""' 2>/dev/null)

# Only gate on files the checks actually cover.
case "$file" in
  *.py|*.toml|*.importlinter|*CONSTRAINTS.md|*STACK.md|"") ;;
  *) exit 0 ;;
esac

if out=$(bash scripts/check_fast.sh 2>&1); then
  rm -f .tmp/last_post_edit_sig
  hlog post_edit 0 0 "$started"
  exit 0
fi

failures=$(echo "$out" | grep -v '^ok    ' | head -15)

# Mid-refactor the same check stays red across several edits. Say so once, then briefly.
mkdir -p .tmp
sig=$(printf '%s' "$failures" | shasum | cut -c1-16)
if [ "$sig" = "$(cat .tmp/last_post_edit_sig 2>/dev/null)" ]; then
  msg="check_fast.sh: same failure as the previous edit ($(echo "$failures" | grep -c '^FAIL') check(s) still red). Run: bash scripts/check_fast.sh"
else
  echo "$sig" > .tmp/last_post_edit_sig
  msg=$(
    echo "check_fast.sh failed after editing ${file:-the working tree}."
    echo "$failures"
    echo
    echo "Fix the code. Do not silence the check, lower a threshold, or edit"
    echo "CONSTRAINTS.md / STACK.md / .importlinter to make this pass."
  )
fi
printf '%s\n' "$msg" >&2
hlog post_edit 2 "${#msg}" "$started"
exit 2
