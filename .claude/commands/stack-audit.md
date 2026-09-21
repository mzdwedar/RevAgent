---
description: Audit the current diff against the ten layers and the six boundary confusions
---

Run the `agent-stack-auditor` subagent against the current change.

1. Establish the diff: `git diff` against the base branch (`main` unless told otherwise).
2. Run the mechanical gates first so the auditor is not asked to do their job:
   `bash scripts/check_task.sh` and `uv run python -m evals run --gates`.
   Report any failures — those are findings in their own right, and the auditor
   should not be reviewing code that does not pass its own bar.
3. Dispatch the `agent-stack-auditor` subagent with the diff, `STACK.md`,
   `CONSTRAINTS.md`, and the names of the fitness tests that already cover this area
   (so it audits the residue, not the covered ground).
4. Return the audit in full. Do not summarize away Critical findings.

This command is mandatory before `/ship`.
