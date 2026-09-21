#!/usr/bin/env bash
# Close the doors around the gates. These are the commands that make a red check
# green without changing anything real.
set -uo pipefail
payload=$(cat)
cmd=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null)

deny() {
  echo "Blocked: $1" >&2
  echo "The gate exists because the bar is not negotiable mid-task. Fix the code," >&2
  echo "or raise the constraint with the human and record the decision." >&2
  exit 2
}

case "$cmd" in
  *--no-verify*)            deny "git --no-verify skips the pre-commit gate." ;;
  *"git commit -n"*)        deny "git commit -n skips the pre-commit gate." ;;
  *--no-cov*)               deny "--no-cov drops the coverage the bar is measured from." ;;
  *"pytest"*"--deselect"*)  deny "deselecting tests hides a failure rather than fixing it." ;;
  *rm*CONSTRAINTS.md*)      deny "CONSTRAINTS.md is the bar; it is not deleted to pass a check." ;;
  *rm*.importlinter*)       deny ".importlinter holds the layer contracts." ;;
  *rm*STACK.md*)            deny "STACK.md is the layer ledger." ;;
esac
exit 0
