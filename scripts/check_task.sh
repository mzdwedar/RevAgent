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

exit $fail
