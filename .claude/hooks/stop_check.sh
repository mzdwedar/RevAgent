#!/usr/bin/env bash
# The turn-end gate: a turn does not end below the bar.
#
# It costs tokens only when it has something to say, so it stays quiet unless this turn
# changed the tree (or the tree is already known red), and it says the least it can.
set -uo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
# shellcheck source=_lib.sh
. "$ROOT/.claude/hooks/_lib.sh"
harness_nested && exit 0
cd "$ROOT" || exit 0
started=$(date +%s)

payload=$(cat)
# Never re-block a turn this hook already blocked once, or the session loops.
if [ "$(printf '%s' "$payload" | jq -r '.stop_hook_active // false' 2>/dev/null)" = "true" ]; then
  exit 0
fi

# Nothing changed, nothing to check.
if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
  exit 0
fi

# A turn that only talked (plan mode, questions, reading) leaves the tree as it found it.
# Skip - unless the last blocked turn left it red, which has to keep blocking.
key=$(bash scripts/tree_key.sh)
if [ "$key" = "$(cat .tmp/turn_start_key 2>/dev/null)" ] && [ "$key" != "$(cat .tmp/red_key 2>/dev/null)" ]; then
  hlog stop_skipped 0 0 "$started"
  exit 0
fi

mkdir -p .tmp
if bash scripts/check_task.sh > .tmp/stop.log 2>&1; then
  rm -f .tmp/red_key
  hlog stop 0 0 "$started"
  exit 0
fi
echo "$key" > .tmp/red_key

{
  echo "check_task.sh failed, so this turn is not done. (full log: .tmp/stop.log)"
  if digest=$(bash scripts/triage_failure.sh .tmp/stop.log); then
    echo "Summary of failures (Haiku digest, advisory - read the log if it is unclear):"
    printf '%s\n' "$digest"
  else
    bash scripts/triage_failure.sh --signal .tmp/stop.log
  fi
} > .tmp/stop.msg
cat .tmp/stop.msg >&2
hlog stop 2 "$(wc -c < .tmp/stop.msg | tr -d " ")" "$started"
exit 2
