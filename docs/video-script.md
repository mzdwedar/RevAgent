# Demo video: script and shot list (3:00)

Every number is from `docs/evidence/` or from the Checkpoint B run. Say nothing that is not
here. Narration is about 400 words, which runs 2:45 to 3:00 at a calm pace.

**Before recording**
- Terminal font large, dark theme. Browser tabs ready: README, `docs/evidence/llm_vs_tabpfn.png`,
  `docs/evidence/cold_start.png`, Temporal UI (`localhost:8233`).
- Have a Checkpoint B run already finished so you can show its output without waiting
  (a live draft turn takes minutes). Say "recorded run" over it, or speed it up on screen.
- Setup for a fresh run: worker on `--task-queue hackathon-checkpoint-b`, then
  `python scripts/checkpoint_b.py` (see its docstring). It needs `data/scores` present.

---

## 0:00 to 0:30 · The headline

| Screen | Narration |
|---|---|
| Title card: "RevAgent: an LLM agent with a tabular brain". Then `llm_vs_tabpfn.png`, cursor on the two bars. | "I'm an indie developer with 200 subscribers. I asked an LLM, qwen3 8B, to find who is about to churn from my table. It scored 0.54 AUC. A coin flip is 0.5. TabPFN, on the same 200 rows, scored 0.84. LLM agents are bad with tables. TabPFN is their tabular brain." |

On screen text: **LLM alone 0.535 · TabPFN 0.842 · same 200 rows, KKBox, 3 seeds**

## 0:30 to 1:05 · Why a foundation model

| Screen | Narration |
|---|---|
| `cold_start.png`, then the n=200 table in the README. Point at KKBox, telecom, bank. | "200 rows is too few to train a model. TabPFN doesn't train: it reads the labelled rows in context. At 200 rows it beats boosted trees by 0.11 on KKBox, 0.17 on telecom and 0.12 on bank. The gap closes as data grows, and on easy data like our synthetic Netflix set there's no gap at all, so I don't claim one. It matters when data is small, which is exactly when a new app needs an answer." |

## 1:05 to 2:20 · The agent uses it, inside guardrails

| Screen | Narration |
|---|---|
| README architecture diagram. Then terminal: run (or replay) `checkpoint_b.py`. Highlight the trigger line. | "RevAgent is an experiment operator. A data trigger starts a durable Temporal run." |
| Highlight the frozen cohort JSON: size 4,987, median risk 0.54, value at risk. | "TabPFN scores the customers and freezes a cohort: 4,987 subscribers, and 7.6 million NTD of annual value at risk. Revenue is observed, never predicted." |
| Highlight the instruction, then the draft in the registry. | "The local LLM then drafts the experiment, grounded in that risk profile: here, a 20 percent discount for high-risk subscribers." |
| Cut to the mistyped-version diff (frozen `...cda1967` vs draft `...c7da1967`), side by side. | "Here's something honest. On my first real run the model mistyped the experiment version, and the run still said success. The draft was attached to a cohort nobody froze. I fixed it: a draft must now match the frozen record, or it's refused and audited." |
| Temporal UI showing the run waiting. | "Nothing reaches a customer until a person approves that exact rollout. The run waits here." |

Do not show or say an approval happened: Checkpoint B stops at the parked approval.

## 2:20 to 3:00 · What's proven, what isn't

| Screen | Narration |
|---|---|
| README "What is not claimed" list. | "What I've shown: the LLM can't rank churn from a table, TabPFN can, and the agent uses it inside a run a human controls. What I haven't: when I gave the LLM TabPFN's score, its AUC recovered only to 0.77, and on one seed it failed. So the claim is that TabPFN should do the ranking, not that the LLM can be trusted to relay it. One dataset, three seeds, 100 test rows." |
| Repo page: README top, `data/open/NOTICE.md`. | "It runs locally on a laptop. Code and every number are in the repo, and the Netflix data is CC0. The licence on TabPFN is non-commercial, and the README says so up front." |
| End card: repo link. | "RevAgent. Let the LLM run the workflow, and let TabPFN read the table." |

---

## Check against the evidence before you record

| Claim | Source |
|---|---|
| 0.535 vs 0.842 AUC | mean of 3 seeds, `llm_vs_tabpfn.csv` |
| 0.77 for LLM given the score; seed 2 = 0.60 | same file |
| Gaps of 0.11, 0.17, 0.12 at n=200 | `cold_start.csv`: KKBox 0.844 vs 0.733; telecom 0.876 vs 0.704; bank 0.814 vs 0.694 (the bank gap is 0.12 rounded) |
| 4,987 customers, 7.6M NTD, median 0.54 | Checkpoint B frozen cohort |
| "Runs locally" | TabPFN-3.5 and qwen3:8b both run on the laptop; the Checkpoint B run used recorded TabPFN scores |

If you cut for time, cut in this order: the cold-start section to one sentence, then the
Netflix aside, then the licence sentence. Never cut the seed-2 admission or the mistyped
version story: they are what make the rest believable.
