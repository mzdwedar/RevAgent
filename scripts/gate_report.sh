#!/usr/bin/env bash
# What /stack-audit's gatekeeper used to do with a model: run the mechanical gates and
# say whether they passed. The verdict is an exit code, so no model has to be trusted
# to report it, and no tokens are spent producing it.
#
#   bash scripts/gate_report.sh [base]      # base defaults to main
#
# Prints, on the last two lines, `GATE STATUS: PASSED|FAILED` and a JSON line with the
# layers the diff touches (scripts/layer_of.py). Failure output is capped; the full log
# is at .tmp/gate.log. Exit 0 only when every gate passed.
set -uo pipefail
cd "$(dirname "$0")/.."
base="${1:-main}"
log=.tmp/gate.log
mkdir -p .tmp/green
: > "$log"

status=0
if [ -z "$(git diff "$base" --stat 2>/dev/null)" ] && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  echo "no changes against $base" | tee -a "$log"
  status=1
fi

bash scripts/check_task.sh >>"$log" 2>&1 || status=1

key=$(bash scripts/tree_key.sh)
marker=".tmp/green/evals.$key"
if [ -f "$marker" ] && [ -z "${CHECK_NO_CACHE:-}" ]; then
  echo "ok    evals gates (cached: this tree $key already passed)" >>"$log"
elif uv run python -m evals run --gates >>"$log" 2>&1; then
  : > "$marker"
else
  status=1
fi

if [ "$status" -ne 0 ]; then
  grep -vE '^ok    ' "$log" | tail -30
  echo "(full log: $log)"
  echo "GATE STATUS: FAILED"
else
  echo "GATE STATUS: PASSED"
fi
uv run python scripts/layer_of.py --base "$base"
exit $status
