#!/usr/bin/env bash
# Put the bar in front of the model at the start of every session, so "read
# CONSTRAINTS.md before writing code" is not a rule it has to remember to follow.
set -uo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

context="This project is built to The Agent Stack (Parts 1-8).

Before writing code:
  1. Read CONSTRAINTS.md. Do not weaken it to make a change pass.
  2. Read STACK.md and name the layer(s) your change touches.
  3. Never collapse two layers to save a file.

Gates that will run whether or not you remember them:
  scripts/check_fast.sh  after every edit (PostToolUse hook, blocking)
  scripts/check_task.sh  at the end of every turn (Stop hook, blocking)
  scripts/stack_guard.py watches the diff for a weakened bar

Layer ledger:
$(sed -n '/^| # | Layer/,/^$/p' "$ROOT/STACK.md" 2>/dev/null | cut -c1-200)

/stack-audit is mandatory before /ship."

if command -v jq >/dev/null 2>&1; then
  jq -cn --arg c "$context" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
else
  echo "$context"
fi
