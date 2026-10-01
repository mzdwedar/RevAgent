#!/usr/bin/env bash
# Put the bar in front of the model at the start of every session, so "read
# CONSTRAINTS.md before writing code" is not a rule it has to remember to follow.
set -uo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

context="This project is built to The Agent Stack.

Before writing code:
  1. Read CONSTRAINTS.md. Do not weaken it to make a change pass.
  2. Name the layer(s) your change touches (table below; STACK.md has the invariants).
  3. Never collapse two layers to save a file.

Gates that will run whether or not you remember them:
  scripts/check_fast.sh  after every edit (PostToolUse hook, blocking)
  scripts/check_task.sh  at the end of every turn (Stop hook, blocking)
  scripts/stack_guard.py watches the diff for a weakened bar

Layers (number | name | owner module) - the invariants live in STACK.md:
$(sed -n '/^| # | Layer/,/^$/p' "$ROOT/STACK.md" 2>/dev/null | awk -F'|' 'NF>4 {print "|" $2 "|" $3 "|" $4 "|"}')

Never collapse: session/authorization, transcript/context, memory/learning,
capability/execution, approval/isolation, observability/evaluation.

Read only what your change needs: 'uv run python scripts/layer_of.py --base main --sections'
names the layers touched and prints their ledger rows.

/stack-audit is mandatory before /ship."

if command -v jq >/dev/null 2>&1; then
  jq -cn --arg c "$context" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
else
  echo "$context"
fi
