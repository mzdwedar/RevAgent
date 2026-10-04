# Hackathon todo — "TabPFN is the agent's tabular brain"

Plan: [`plan-hackathon.md`](plan-hackathon.md). Cut order if short: T4, then T1 arm (c), then the abstention scene.

## Phase 1 — Go/no-go evidence
- [x] T1. `scripts/llm_vs_tabpfn.py`: qwen3:8b alone vs TabPFN-3.5 vs LLM shown TabPFN, KKBox only (telecom not run, by choice), n=200, 100 test rows, 3 seeds
  - [x] AUC and revenue captured in the top 10%, mean ± sd per arm; malformed LLM output counted, not dropped; resumes from CSV
  - [x] Smoke run (1 seed, 20 rows); ruff clean; TabPFN arm ≈ cold-start CSV at n=200
  - [x] Go/no-go: LLM-alone more than 0.05 AUC below TabPFN, else stop and reframe
- [x] T2. Commit `scripts/cold_start_curve.py`, `docs/evidence/cold_start.*`, T1 outputs (no raw rows)

### Checkpoint A
- [x] Review the T1 numbers with the user

## Phase 2 — The agent uses TabPFN through tools
- [~] T3 (cut by decision). `get_cohort_risk` read tool returning `Cohort.description()`; no inference inside the tool. Redundant: the draft turn is already told the cohort profile (0ca9238)
  - [ ] Listed by `build_registry()`; fitness tests unchanged and passing; unit test; `check_fast.sh` green
- [~] T4 (cut, first cut in the plan). TabPFN uncertainty (spread over k seeds) in the cohort payload + abstention threshold in `experiments/targeting.toml`
  - [ ] Threshold in rule fingerprint; forced high-uncertainty run abstains; normal run unchanged; `check_task.sh` green

### Checkpoint B
- [x] Local end-to-end run on KKBox with real qwen3:8b: trigger → frozen cohort → grounded draft in the registry (`scripts/checkpoint_b.py`). First run exposed a mistyped experiment version, fixed by draft admission (ab31d35); re-run after the fix landed the exact frozen version, 1 receipt, 0 refusals

## Phase 3 — Story and submission
- [x] T5. Judge-first README section (pitch, thesis, both plots + numbers, diagram, video + revbench links)
- [ ] T6. 3-minute video (script + shot list from me; headline number in the first 30 s)
- [ ] T7. Push, make the repo public, submit RevAgent; submit revbench as a second entry
