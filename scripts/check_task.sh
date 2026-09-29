#!/usr/bin/env bash
# The turn-end gate. Budget: under 90 seconds.
#
# Everything in check_fast.sh, plus the full architecture suite, coverage of the lines
# this change touched, the guard that asks whether the bar itself was weakened, and the
# two that ask whether a deploy would strand live runs: the turn graph's checkpoints and
# the workflow's recorded histories. Replaying those takes about a second, so it runs at
# every turn end rather than waiting for CI.
set -uo pipefail
cd "$(dirname "$0")/.."

# One green run per tree. The Stop hook, the gatekeeper and check_full.sh all reach this
# script; when the tree is byte-identical to one that already passed, say so and stop.
# Only a pass is remembered, so a red tree always re-runs. CHECK_NO_CACHE=1 forces it.
key=$(bash scripts/tree_key.sh)
marker=".tmp/green/task.$key"
if [ -z "${CHECK_NO_CACHE:-}" ] && [ -f "$marker" ]; then
  echo "ok    check_task (cached: this tree $key already passed)"
  exit 0
fi

fail=0
bash scripts/check_fast.sh || fail=1

echo "----- tests + coverage"
if uv run pytest --cov=agentstack --cov-report=lcov --cov-report=term-missing:skip-covered -q; then
  echo "ok    tests"
else
  echo "FAIL  tests"
  fail=1
fi

echo "----- changed-line coverage"
uv run python scripts/changed_line_coverage.py --min 80 || fail=1

echo "----- bar integrity"
uv run python scripts/stack_guard.py || fail=1

echo "----- checkpoint compatibility"
uv run python scripts/checkpoint_guard.py || fail=1

echo "----- workflow replay"
uv run python scripts/replay_guard.py || fail=1

if [ "$fail" -eq 0 ]; then
  mkdir -p .tmp/green && : > "$marker"
fi
exit $fail
