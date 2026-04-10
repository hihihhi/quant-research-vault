# Quant Research Vault

An autonomous quantitative knowledge brain — ingests 18,000+ research papers across quant finance,
econophysics, ML-for-finance, and RL-for-trading. Distills insights into structured methodology
docs, and makes everything searchable via MCP (Claude Code), CLI, and Copilot context.

## What it does

- Ingests papers from **arXiv**, **OpenAlex**, and **Semantic Scholar** across 4 domain profiles
- Generates structured trading analysis per paper (signal construction, Sharpe, failure modes, crypto/HK applicability)
- Distills all papers into 14 methodology topics (Opus-planned, Sonnet-written synthesis docs)
- Makes knowledge searchable via MCP (Claude Code), CLI, and Copilot context
- Auto-updates daily and re-distills as the vault grows

---

## Quick Start

```bash
git clone https://github.com/hihihhi/quant-research-vault
cd quant-research-vault
python install.py          # deps, dirs, DB init, MCP registration, Task Scheduler
```

### Check health first
```bash
python test_repo.py        # self-diagnostic — verifies entire stack without AI
```

### Autonomous full build (recommended)
```bash
python master.py           # fully autonomous: arXiv + OpenAlex + SS → distill → Phase 2 analysis
```
master.py saves progress to `.db/master_progress.json` — safe to kill and restart at any point.

### Manual phase-by-phase
```bash
# Phase 1: Index all papers via abstracts (fast, ~2 hours for 18k papers)
python run.py --all-history --abstract-only

# Phase 2: Full Claude analysis in background (slow, days — runs autonomously)
python run.py --all-history --workers 3

# Distill vault into 14 methodology topic docs
python research.py --distill
```

### Monitor progress
```bash
python master.py --status    # paper count, phase progress, distillation quality
python test_repo.py          # full health check with fix hints
```

---

## Domain Profiles

Enable/disable entire research domains in `config.yaml` under `profiles:`:

| Profile | Categories | Filter |
|---------|-----------|--------|
| `quant_finance` | q-fin.PM, TR, ST, RM, CP, MF | None — include all |
| `ml_for_finance` | stat.ML, cs.LG | Finance/trading keywords |
| `econophysics` | physics.soc-ph, cond-mat.stat-mech | Econophysics keywords |
| `reinforcement_learning` | cs.AI, cs.LG | RL + trading keywords |

To disable a domain: set `enabled: false` in `config.yaml`. Add new domains by following the same pattern.

---

## Claude Code (MCP)

Registered automatically by `install.py`. Available tools in Claude Code:

```
search_papers("momentum factor crypto")     — semantic search over vault
list_recent_papers(7)                        — papers from last N days
get_paper("2401.12345")                     — full paper analysis
generate_alpha_ideas("pairs trading")        — AI alpha ideas from vault context
get_vault_stats()                            — total papers, date range, index size
```

---

## CLI Research (`research.py`)

```bash
# Semantic search (searches 18k+ papers via ChromaDB)
python research.py "volatility forecasting"

# Snapshot: synthesize vault knowledge on a specific topic
python research.py --snapshot "crypto funding rate RL"
# Output: guidelines/snapshots/crypto-funding-rate-rl-YYYYMMDD.md

# Generate alpha ideas via Claude (top 8 relevant papers as context)
python research.py --alpha-ideas "crypto momentum"

# Vault statistics
python research.py --stats

# Today's new papers
python research.py --daily

# Find related papers for a specific arXiv ID
python research.py --related 2401.12345

# Top N papers by crypto applicability score (requires Phase 2)
python research.py --top-crypto 10

# Export search results to markdown
python research.py --export "momentum factor" out.md

# Distill all 14 topics (Opus+Sonnet, ~30 min)
python research.py --distill
```

---

## Vault Structure

```
~/Documents/ClaudeVault/
├── research/
│   ├── q-fin.TR/          # Trading & Microstructure
│   ├── q-fin.PM/          # Portfolio Management
│   ├── q-fin.ST/          # Statistical Finance
│   ├── q-fin.RM/          # Risk Management
│   ├── q-fin.CP/          # Computational Finance
│   ├── q-fin.MF/          # Mathematical Finance
│   ├── stat.ML/           # ML-for-finance papers
│   ├── cs.LG/             # Deep learning / RL papers
│   ├── physics.soc-ph/    # Econophysics papers
│   └── cond-mat.stat-mech/
├── guidelines/
│   ├── quant-methodology-distilled.md   # 14-topic synthesis (main brain)
│   └── snapshots/                       # On-demand topic snapshots
└── ...
```

Each paper file (Phase 2 analyzed) contains:
- Full abstract
- Signal / Alpha Idea
- Construction recipe (formulas, inputs)
- Key parameters (lookback, thresholds, rebalance)
- Empirical results (exact Sharpe, t-stat, sample period)
- Failure modes and regime conditions
- Crypto applicability (1–5) + HK Equity applicability (1–5)
- Implementation checklist

---

## Configuration (`config.yaml`)

| Key | Default | Description |
|-----|---------|-------------|
| `vault_path` | `~/Documents/ClaudeVault` | Vault location — supports `~`, works cross-platform |
| `profiles` | 4 enabled | Domain profiles — set `enabled: false` to exclude a domain |
| `claude_model` | `claude-haiku-4-5-20251001` | Model for paper summarization (Phase 2) |
| `days_lookback` | `14` | Days back for daily pipeline runs |
| `fetch_pdf` | `true` | Download full PDF for Phase 2 (disable to save bandwidth) |
| `max_pdf_chars` | `12000` | Max chars sent to Claude from PDF |
| `extra_sources.openalex.enabled` | `true` | Include OpenAlex non-arXiv papers |
| `extra_sources.semantic_scholar.enabled` | `false` | Bulk SS import (run manually) |

---

## Pipeline Scripts

| Script | Purpose |
|--------|---------|
| `master.py` | **Autonomous orchestrator** — runs all phases, re-distills at milestones |
| `install.py` | One-command setup: deps, dirs, DB, MCP wiring, Task Scheduler |
| `fetch.py` | Pull paper metadata from arXiv + OpenAlex + Semantic Scholar |
| `process.py` | Build vault files (Phase 1: abstract-only, Phase 2: full Claude) |
| `sync.py` | Index vault into ChromaDB for semantic search |
| `run.py` | Orchestrates fetch + process + sync for daily or history mode |
| `research.py` | CLI: search, snapshot, alpha ideas, distill |
| `search_mcp.py` | MCP server — exposes vault to Claude Code |
| `test_repo.py` | Self-diagnostic — verifies entire stack (run after install or issues) |

---

## Notices & Known Behaviours

### Rate limits
- arXiv API rate-limits aggressively. The pipeline uses `delay_seconds=5` between requests
  and exponential backoff (waits up to 480s on 429). Do not run multiple `fetch.py` instances simultaneously.

### Phase 2 takes days
- Full Claude analysis on 18k papers takes 5–10 days at 2–3 workers.
  `master.py` runs this autonomously in background; safe to leave running overnight/across days.

### Workers cap
- Workers are auto-capped based on available RAM and CPU (max 5 hard limit).
  Each Claude worker spawns a Node.js process (~300–500 MB). Running too many
  workers will make your PC unresponsive. Default auto-detection is safe.

### abstract-only vs fully analyzed
- Phase 1 (`--abstract-only`) indexes papers immediately but without signal analysis.
  Phase 2 upgrades them with full Claude output. The vault is searchable after Phase 1.
  Papers in Phase 1 state will not appear in `--top-crypto` or signal queries.

### OpenAlex IDs
- Non-arXiv papers from OpenAlex use `oa:W...` as their ID. These are stored in the
  same SQLite DB and searchable alongside arXiv papers.

### master.py progress file
- `.db/master_progress.json` tracks pipeline phase. Delete it to restart from scratch,
  or leave it to resume from the last completed phase.

### Distillation requires ChromaDB
- `--distill` queries ChromaDB for relevant papers per topic. Run `python sync.py`
  first if you see "index is empty" errors.

### Cross-platform paths
- `vault_path` uses `~/Documents/ClaudeVault` (expanduser). Works on Windows, Mac, Linux.
  Do NOT use absolute paths with hardcoded usernames in config.yaml.

---

## Requirements

- Python >= 3.11
- `claude` CLI installed (Anthropic Max plan) — for paper analysis and distillation
- Install dependencies: `pip install -r requirements.txt`

```
arxiv, chromadb, pdfplumber, requests, psutil, pyyaml, mcp[cli]
```
