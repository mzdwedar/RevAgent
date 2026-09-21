#!/usr/bin/env bash
# The turn-end gate. Budget: under 90 seconds.
#
# Everything in check_fast.sh, plus the full architecture suite, coverage of the lines
# this change touched, and the guard that asks whether the bar itself was weakened.
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

exit $fail
