---
description: Audit the current diff against the ten layers and the six boundary confusions
---

1. **Step 1: Dispatch Gatekeeper (Haiku)**
   Run the `gatekeeper` subagent to run the mechanical checks
   (`bash scripts/check_task.sh` and `uv run python -m evals run --gates`) and
   establish that a `git diff` against the base branch (`main` unless told otherwise) exists.

2. **Step 2: Early Abort Gate**
   Check the output from `gatekeeper`. If `GATE STATUS: FAILED`, STOP immediately.
   Report the failures as findings. Do NOT dispatch the auditor on code that fails
   its own bar.

3. **Step 3: Dispatch Architectural Auditor (Opus)**
   If `GATE STATUS: PASSED`, dispatch the `agent-stack-auditor` subagent with:
   - The git diff output
   - `STACK.md` and `CONSTRAINTS.md`
   - The names/files of existing fitness tests covering this area, so it audits only
     the uncovered residue

4. **Step 4: Output Audit**
   Return the audit in full. Do not summarize away Critical findings.

This command is mandatory before `/ship`.
