# Plan: RevAgent as the TabPFN-3.5 hackathon entry — "TabPFN is the agent's tabular brain"

## Context
Goal: 1st place in Prior Labs' TabPFN-3.5 hackathon, judged by a panel. Time left: about one working day. Agreed direction:
- **Thesis:** LLM agents are bad with tables; TabPFN-3.5 is their tabular brain.
- **Proof 1:** an LLM ranking churn risk on its own vs the same LLM using TabPFN, on the same rows.
- **Proof 2:** the RevAgent agent uses TabPFN in more than one role. The KKBox cold-start curve is supporting evidence.
- **Story:** "retention science on day one for an indie developer with 200 subscribers."
- **Submission:** RevAgent is the main entry; revbench is linked as a second entry.

Already done (the hackathon worktree, branch `hackathon-cold-start`, uncommitted):
- `scripts/cold_start_curve.py`
- `docs/evidence/cold_start.{csv,png}`: 270 fits; TabPFN-3.5 beats boosted trees and logistic regression at every n on telecom, bank and KKBox. KKBox n=200: AUC 0.844 vs 0.733; top-10% captures 81% vs 61% of churned revenue.

### Constraints found in the code
- The main checkout is shared with other sessions. All work stays in the hackathon worktree; commit only our hunks.
- `tasks/todo.md` and `tasks/todo-kkbox-cohort.md` each have 16 unchecked tasks from other work. **Do not overwrite them.** The breakdown goes to `tasks/plan-hackathon.md` + `tasks/todo-hackathon.md`, following the existing `-kkbox-cohort` naming.
- **Gates:** `scripts/check_fast.sh` runs ruff, format, strict mypy on `agentstack`, import-linter layer contracts and fitness tests. Tools must declare complete `ToolSpec` metadata (`src/agentstack/tools/spec.py`; checked by `tests/fitness/test_tool_registry.py`).
- **The LLM already orchestrates:** `OllamaEngine` (`src/agentstack/model/ollama_engine.py`, `qwen3:8b`, installed locally) proposes tool calls from `runtime/nodes.py:call_model`. TabPFN scoring is a fixed pipeline step (`prediction/engine.py`, `context/targeting.py`) that the LLM never sees through a tool.
- **No predicted revenue, by design:** value at risk is observed (`targeting.annual_revenue_cents`), so LTV regression is out. No `tabpfn-time-series` package is installed and no daily series is on disk. KKBox transaction history exists only inside `data/kkbox-raw/_labelled_history.pkl` (1.9 GB).

## Architecture decisions
1. **Proof 1 is a script, not package code** (`scripts/llm_vs_tabpfn.py`). It reuses `OllamaEngine` and `prediction.engine.encode`/`CHECKPOINT`, and the cold-start split helpers from `scripts/cold_start_curve.py`. This keeps it outside the layer gates so it ships fast.
2. **TabPFN reaches the LLM as a read-only tool**, `get_cohort_risk`. It returns the frozen cohort's TabPFN risk summary that already exists: `Cohort.description()` — risk quantiles, threshold, size, observed value at risk, model version. **The tool does not run inference.** Scoring stays a pipeline step, which keeps the stack's "no computation or side effects inside tools" rule. The pattern is `_read(...)` in `tools/experiments.py`: no approval, natural idempotency.
3. **Second TabPFN role = uncertainty-aware abstention** (default; see Open questions). TabPFN's spread across seeds / a small ensemble gives a per-cohort uncertainty, which goes into the `get_cohort_risk` payload. The policy and the LLM use the existing `record_abstention` tool when the uncertainty is too high. It uses existing tools and no new data. Alternative: a forecast trigger built from the KKBox history pickle (higher wow, ~2× the risk).

## Tasks (ordered; high-risk first)

### Phase 1 — Go/no-go evidence
**T1. Proof 1 script: LLM alone vs TabPFN on the same rows** (M, ~3–4h)
- **What:** for KKBox and telecom, n=200 training rows and 100 test rows, 3 seeds. Arms:
  - (a) `qwen3:8b`, given the training rows as CSV in the prompt, returns a 0–100 churn risk for each test row (JSON, validated);
  - (b) TabPFN-3.5 on the same rows;
  - (c) the LLM shown TabPFN's probabilities, which must not degrade them.
  Outputs `docs/evidence/llm_vs_tabpfn.{csv,png}`.
- **Accept:**
  - AUC and revenue captured in the top 10% per arm, as mean ± sd;
  - malformed LLM output is counted and reported, not dropped silently;
  - the run resumes from the CSV.
- **Verify:** smoke run with 1 seed and 20 test rows; ruff clean; the numbers are plausible (TabPFN arm ≈ the cold-start CSV at n=200).
- **Go/no-go:** if the LLM-alone arm is within 0.05 AUC of TabPFN, the thesis is weak. Stop and reframe with the user.

**T2. Commit the cold-start evidence** (XS)
- **What:** commit `scripts/cold_start_curve.py`, `docs/evidence/cold_start.{csv,png}` and the T1 outputs. No raw rows.
- **Accept:** `git diff --cached` contains only those files.
- **Verify:** ruff passes; `--plot-only` regenerates the PNG from the CSV.

**Checkpoint A:** review the T1 numbers with the user before building on the thesis.

### Phase 2 — The agent uses TabPFN through tools
**T3. `get_cohort_risk` read tool** (M)
- **What:**
  - a new `ToolSpec` in `src/agentstack/tools/` (scope e.g. `cohort:read`, `Surface.REGISTRY` or a new read-only surface, whichever the sandbox rules allow without weakening them);
  - a read handler that returns `Cohort.description()` for the run's frozen cohort;
  - exposure on the draft and evaluation stages.
- **Accept:**
  - the tool is listed by `build_registry()`;
  - the fitness tests pass unchanged;
  - the result enters context as an observation with provenance;
  - no TabPFN call happens inside the tool.
- **Verify:** a new unit test under `tests/`; `scripts/check_fast.sh` passes.

**T4. TabPFN uncertainty in the cohort payload + abstention rule** (M)
- **What:**
  - `TabPFNScorer` (or a thin wrapper) records the per-row spread over k seeds, recorded once alongside the scores;
  - the cohort description gains `uncertainty` (e.g. the mean sd in the top decile);
  - the policy and the prompt instruct `record_abstention` when it exceeds a threshold in `experiments/targeting.toml`.
- **Accept:**
  - the threshold lives in the toml and is folded into the rule fingerprint;
  - a run with a forced high-uncertainty cohort abstains;
  - the normal run is unchanged.
- **Verify:** unit tests for both branches; `check_fast.sh` + `check_task.sh` pass.

**Checkpoint B:** an end-to-end local run (`scripts/dev_up.sh`, `uv run agentstack`) shows the LLM calling `get_cohort_risk` and then drafting or abstaining. The traces show the tool call.

### Phase 3 — Story and submission
**T5. Judge-first README section** (S, ~1.5h)
- **What:** add at the top of README.md:
  - the pitch (indie developer, 200 subscribers);
  - the thesis;
  - the Proof 1 plot + number;
  - the cold-start plot + KKBox number;
  - a "how TabPFN drives the agent" diagram (risk → `get_cohort_risk` → draft or abstain);
  - links to the video and revbench.

  Existing sections move below, unchanged.
- **Accept:** the headline numbers come from the committed CSVs; no claim lacks evidence.
- **Verify:** fresh clone of the branch; every link and image resolves.

**T6. 3-minute video** (~2.5h, user-led; I prepare the script + shot list)
- **What:**
  - 0:00–0:20 hook + Proof 1 number;
  - the cold-start plot;
  - the live run: trigger → LLM calls `get_cohort_risk` → draft → Slack approval → single rollout → deduplicated retry;
  - an abstention case;
  - architecture in 20 seconds.
- **Accept:** the headline number appears within 30 seconds.

**T7. Submit** (XS)
- **What:**
  - merge or push the branch;
  - make the repo public;
  - fill in the form (description leads with the thesis + 2 numbers; links to the video and revbench);
  - submit revbench as a separate entry.

**Cut order if time runs short:** T4 first (keep T3 with risk only), then the LLM+TabPFN arm (c) in T1, then the abstention scene in the video.

## Risks
| Risk | Impact | Mitigation |
|---|---|---|
| The LLM-alone arm is not much worse | High (the thesis fails) | T1 runs first as a go/no-go; fallback thesis is "cold start", with evidence already in hand |
| qwen3:8b output is malformed or slow on 200-row prompts | Med | JSON validation + retry once; count failures; keep the prompt to ~200 rows × ~15 columns (~20k tokens; check the context window in `OllamaEngine.asset`) |
| Judges object "a small LLM is a strawman" | Med | State it; optionally add one stronger API model run if the user wants to pay for it |
| Layer and fitness gates slow T3/T4 | Med | Follow the `_read` pattern exactly; never weaken a gate; cut T4 first |
| Shared worktree collisions | Low | Separate worktree; commit only our files |

## Decisions confirmed with the user
- Second TabPFN role: **uncertainty abstention** (T4). No forecast trigger.
- Proof 1 LLM: **local `qwen3:8b` only**. The README states it is an 8B model.
- Time left: **about 1 day**. Full plan T1–T7; T4 is cut first if needed.

## Verification (end to end)
- `scripts/check_fast.sh` and `scripts/check_task.sh` pass on the branch, with no gate weakened.
- `uv run python scripts/cold_start_curve.py --plot-only` and `scripts/llm_vs_tabpfn.py --plot-only` regenerate the figures from the committed CSVs.
- A local end-to-end run shows the `get_cohort_risk` tool call in the traces, then a draft → approval → one rollout. A forced high-uncertainty cohort abstains.
- Fresh clone: the README renders, all figures and links resolve.
- On approval, this breakdown is written to `tasks/plan-hackathon.md` and `tasks/todo-hackathon.md` in the worktree.
