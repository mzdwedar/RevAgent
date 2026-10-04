# Hackathon todo — "TabPFN is the agent's tabular brain"

Plan: [`plan-hackathon.md`](plan-hackathon.md). Cut order if short: T4, then T1 arm (c), then the abstention scene.

## Phase 1 — Go/no-go evidence
- [ ] T1. `scripts/llm_vs_tabpfn.py`: qwen3:8b alone vs TabPFN-3.5 vs LLM shown TabPFN, KKBox + telecom, n=200, 100 test rows, 3 seeds
  - [ ] AUC and revenue captured in the top 10%, mean ± sd per arm; malformed LLM output counted, not dropped; resumes from CSV
  - [ ] Smoke run (1 seed, 20 rows); ruff clean; TabPFN arm ≈ cold-start CSV at n=200
  - [ ] Go/no-go: LLM-alone more than 0.05 AUC below TabPFN, else stop and reframe
- [ ] T2. Commit `scripts/cold_start_curve.py`, `docs/evidence/cold_start.*`, T1 outputs (no raw rows)

### Checkpoint A
- [ ] Review the T1 numbers with the user

## Phase 2 — The agent uses TabPFN through tools
- [ ] T3. `get_cohort_risk` read tool returning `Cohort.description()`; no inference inside the tool
  - [ ] Listed by `build_registry()`; fitness tests unchanged and passing; unit test; `check_fast.sh` green
- [ ] T4. TabPFN uncertainty (spread over k seeds) in the cohort payload + abstention threshold in `experiments/targeting.toml`
  - [ ] Threshold in rule fingerprint; forced high-uncertainty run abstains; normal run unchanged; `check_task.sh` green

### Checkpoint B
- [ ] Local end-to-end run: trace shows LLM → `get_cohort_risk` → draft or abstain

## Phase 3 — Story and submission
- [ ] T5. Judge-first README section (pitch, thesis, both plots + numbers, diagram, video + revbench links)
- [ ] T6. 3-minute video (script + shot list from me; headline number in the first 30 s)
- [ ] T7. Push, make the repo public, submit RevAgent; submit revbench as a second entry
