---
description: Audit the current diff against the ten layers and the six boundary confusions
---

1. **Step 1: Dispatch Gatekeeper (Haiku)**
   Run the `gatekeeper` subagent. It runs `bash scripts/gate_report.sh` (mechanical
   checks and eval gates, cached per tree, so a tree the Stop hook just verified is not
   re-run) and relays the output verbatim. The status is computed by exit codes.

2. **Step 2: Early Abort Gate**
   If the output says `GATE STATUS: FAILED`, STOP immediately. Report the failures as
   findings. Do NOT dispatch the auditor on code that fails its own bar.

3. **Step 3: Decide whether an architectural audit is needed**
   Read the JSON line the gatekeeper printed.
   - `src_changed: false` and `unmapped_non_doc_files: []` (docs/tests only): there is
     nothing architectural to audit. Say so, and skip Step 4.
   - Otherwise continue. `unmapped_non_doc_files` (migrations, evals, scripts, config)
     have no layer; give them to the auditor as-is.

4. **Step 4: Dispatch Architectural Auditor (Opus)**
   Dispatch `agent-stack-auditor` with:
   - `git diff main` (or the given base)
   - The output of `uv run python scripts/layer_of.py --base main --sections`: the
     layers touched and their ledger rows. The auditor reads the rest of `STACK.md`
     and `CONSTRAINTS.md` only if a finding needs it.
   - The names of existing fitness tests covering this area, so it audits only the
     uncovered residue

5. **Step 5: Output Audit**
   Return the audit in full. Do not summarize away Critical findings.

This command is mandatory before `/ship`.
