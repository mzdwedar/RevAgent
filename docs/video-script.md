# Demo video: script and shot list (about 2:00)

Every number is from `docs/evidence/`. Say nothing that is not
here. Narration is about 270 words, which runs about 2:00 at a calm pace.

**Before recording**
- Terminal font large, dark theme. Browser tabs ready: README, `docs/evidence/llm_vs_tabpfn.png`,
  `docs/evidence/cold_start.png`.

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

## 1:05 to 2:00 · What's proven, what isn't

| Screen | Narration |
|---|---|
| README "What is not claimed" list. | "What I've shown: the LLM can't rank churn from a table, and TabPFN can. What I haven't: when I gave the LLM TabPFN's score, its AUC recovered only to 0.77, and on one seed it failed. So the claim is that TabPFN should do the ranking, not that the LLM can be trusted to relay it. One dataset, three seeds, 100 test rows." |
| Repo page: README top, `data/open/NOTICE.md`. | "It runs locally on a laptop. Code and every number are in the repo, and the Netflix data is CC0. The licence on TabPFN is non-commercial, and the README says so up front." |
| End card: repo link. | "RevAgent. Let the LLM run the workflow, and let TabPFN read the table." |

---

## Check against the evidence before you record

| Claim | Source |
|---|---|
| 0.535 vs 0.842 AUC | mean of 3 seeds, `llm_vs_tabpfn.csv` |
| 0.77 for LLM given the score; seed 2 = 0.60 | same file |
| Gaps of 0.11, 0.17, 0.12 at n=200 | `cold_start.csv`: KKBox 0.844 vs 0.733; telecom 0.876 vs 0.704; bank 0.814 vs 0.694 (the bank gap is 0.12 rounded) |
| "Runs locally" | TabPFN-3.5 and qwen3:8b both run on the laptop |

If you cut for time, cut in this order: the cold-start section to one sentence, then the
Netflix aside, then the licence sentence. Never cut the seed-2 admission: it is what
makes the rest believable.
