# ANALYSIS_SKILL — Full Paper Analysis via Claude Code

This file tells Claude Code how to analyze papers in the quant-research-vault.
**No subprocess spawning. No zombie Node.js processes.**
Claude Code (this instance) does the analysis directly using multi-agent teams.

---

## When to use

Run this skill when:
- `python list_pending.py --count-only` returns > 0
- User asks to "analyze papers", "process vault", "upgrade abstracts"
- User asks to enrich a specific paper: "analyze 2401.12345"

---

## Workflow

### Step 1 — Check what's pending
```bash
python list_pending.py --count-only           # total count
python list_pending.py --limit 20             # first 20 as JSON
python list_pending.py --domain q-fin --limit 10   # filter by domain
```

### Step 2 — Analyze a batch (multi-agent)

For each paper in the batch, spawn parallel agents:

**Agent A (Reader)** — Explore agent, haiku model
- Read the vault file at `paper["vault_path"]`
- Fetch PDF if `pdf_url` available (download to temp, extract text)
- Return: abstract + key PDF excerpts (intro, methods, results)

**Agent B (Analyst)** — Sonnet agent
- Receives paper text from Agent A
- Writes the full structured analysis using the prompt below
- Returns the markdown sections

**Agent C (Reviewer)** — separate Sonnet agent
- Reviews Agent B's output for completeness and accuracy
- Checks: Sharpe numbers present? Signal direction clear? Crypto applicability scored?
- Returns: approved or specific revision requests

After approval, write the full entry to the vault file and mark analyzed:

```bash
python mark_analyzed.py <arxiv_id>
```

### Step 3 — Sync and distill after each batch
```bash
python sync.py        # index new analyses into ChromaDB
```

After 500+ papers analyzed, optionally re-distill:
```bash
python research.py --distill
```

---

## Analysis Prompt

Use this exact prompt structure when analyzing each paper:

```
You are the quantitative brain of an autonomous trading firm specializing in
crypto and HK equities. Extract every exploitable insight from this research
paper and turn it into actionable intelligence for our signals team.

Paper text:
---
{abstract + pdf_text}
---

Output ONLY the following markdown (no preamble, no trailing commentary):

## Signal / Alpha Idea
One paragraph: what trading signal, factor, or strategy does this paper propose?
Be specific about directionality, holding period, asset class.

## Construction
Exact recipe: inputs required, calculation steps, key formulas. Be concrete enough
that a quant dev could implement this from your description alone.

## Key Parameters
| Parameter | Value | Notes |
|-----------|-------|-------|
(lookback windows, thresholds, rebalance frequency, hyperparameters)

## Empirical Results
- Best reported Sharpe / return / hit rate (exact number + sample period)
- Statistical significance (t-stat, p-value, bootstrap CI if reported)
- Transaction cost assumption used in the paper

## Failure Modes & Risks
- When does this alpha decay or reverse? (market regime, crowding, etc.)
- Data requirements that may be unavailable or expensive
- Overfitting / publication bias concerns

## Regime Conditions
Bull/bear, high/low vol, trending/mean-reverting — when is this strongest / weakest?

## Crypto Applicability
Rate 1-5 and explain: can this be adapted for BTC/ETH/altcoin markets?
What adaptations are needed for 24/7 markets, thin books, higher vol?

## HK Equity Applicability
Rate 1-5 and explain: relevant for HKEX / H-shares / Hang Seng constituents?

## Implementation Checklist
- [ ] Data source required
- [ ] Compute complexity (O(n), realtime vs batch)
- [ ] Infrastructure dependencies
- [ ] Estimated time to implement

## Relevance Score
Novelty: X/5 | Feasibility: X/5 | Crypto: X/5 | HK Equity: X/5

*(Use rubric below — cite "ANALYSIS_SKILL v1" when scoring)*
```

---

## Scoring Rubric (v1) — cite this before assigning any score

Scores without this rubric are meaningless. Every paper analysis MUST reference "v1" and apply these criteria exactly.

### Novelty (how original is the contribution?)
| Score | Criteria |
|-------|----------|
| 5/5 | Genuinely new mechanism; first-principles derivation with natural experiment; opens a new research direction |
| 4/5 | New application of known technique with substantial original contribution; new asset class + significant modification |
| 3/5 | Incremental improvement on existing method; new dataset but similar technique |
| 2/5 | Marginal variant of widely-known approach; engineering contribution only |
| 1/5 | Restatement of textbook material; no novel contribution |

### Feasibility (given: Python + standard quant infra + Bloomberg/Refinitiv + $10K–$10M AUM)
| Score | Criteria |
|-------|----------|
| 5/5 | Deploy within 1 week; off-shelf libraries; public data; positive live-market track record |
| 4/5 | 2–4 week implementation; standard data sources; minor calibration |
| 3/5 | 1–2 month build; specialized data (LOB, alt data); significant engineering |
| 2/5 | 3–6 month build; expensive proprietary data; ML/infra expertise required |
| 1/5 | Research-only; quantum/satellite data/institutional-only infra required |

### Crypto Applicability (primary: BTC/ETH/SOL major perps; secondary: mid-cap alts)
| Score | Criteria |
|-------|----------|
| 5/5 | Tested on crypto OR direct mechanic applies; minimal adaptations; positive P&L demonstrated |
| 4/5 | High structural fit; minor adaptations for 24/7, funding rate, thin books; likely profitable after calibration |
| 3/5 | Moderate fit; significant adaptations required; plausible but unproven on crypto |
| 2/5 | Weak fit; mechanism partially applicable; most adaptations create new untested assumptions |
| 1/5 | Not applicable; mechanism specific to equities/rates/other asset class |

### HK Equity Applicability (primary: HSI constituents, H-shares, HKEX listed large-cap)
| Score | Criteria |
|-------|----------|
| 5/5 | Tested on HK/Asia equities OR directly analogous; regulatory constraints (SFC, stamp duty) satisfied |
| 4/5 | High fit; minor adaptations for HKEX microstructure, lot sizes, stamp duty (0.1%/side) |
| 3/5 | Moderate fit; requires HK-specific recalibration; plausible on liquid HSI names |
| 2/5 | Weak fit; structural frictions (stamp duty, SFC short-selling list, T+2) significantly impair the strategy |
| 1/5 | Not applicable to HK equity structure |

---

## Vault file format

The vault file already has the header + abstract (from Phase 1). Append the analysis after the abstract:

```markdown
---
arxiv_id: ...
title: "..."
...
---

# Title

**Authors:** ...

## Abstract

{abstract}

---

## Signal / Alpha Idea
...
(rest of analysis)
```

To get the existing file content: `Read vault_path`
To write the updated content: `Edit vault_path` — append analysis after `## Abstract\n\n{abstract}\n`

---

## Batch size guidance

| Available RAM | Batch size | Parallel agents |
|---------------|------------|-----------------|
| < 8 GB        | 5 papers   | 2 per paper     |
| 8–16 GB       | 15 papers  | 3 per paper     |
| > 16 GB       | 30 papers  | 3 per paper     |

Always Generator ≠ Evaluator. Never let the same agent write and review its own output.

---

## Domain priority order

Process in this order for maximum trading signal value:
1. `q-fin` (quant finance — direct alpha signals)
2. `cs.LG` / `stat.ML` with RL keywords (RL trading agents)
3. `physics.soc-ph` / `cond-mat` (econophysics — regime/structure insights)
4. `cs.LG` / `stat.ML` general ML (methodology papers)

---

## Quick start

```bash
# Check what's pending
python list_pending.py --count-only

# Get a small batch to start
python list_pending.py --domain q-fin --limit 10 > /tmp/batch.json

# After processing batch, sync
python sync.py

# Check progress
python master.py --status
```
