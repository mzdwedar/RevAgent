#!/usr/bin/env bash
# UserPromptSubmit: remember the tree as it stood when the turn began, so the Stop hook
# can tell a turn that changed code from one that only talked.
set -uo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
# shellcheck source=_lib.sh
. "$ROOT/.claude/hooks/_lib.sh"
harness_nested && exit 0
cd "$ROOT" || exit 0
mkdir -p .tmp
bash scripts/tree_key.sh > .tmp/turn_start_key 2>/dev/null
exit 0
