# Quant Research Vault

A self-contained quantitative brain that automatically ingests all arXiv quant finance papers (1997 to present) with full Claude-generated trading analysis, and makes this knowledge searchable via MCP (Claude Code), GitHub Copilot context, and a research CLI.

## What it does

- Ingests ALL arXiv quant finance papers (1997 to present, ~40,000+ papers)
- Generates structured trading analysis for each paper (Claude-powered)
- Makes the knowledge searchable via MCP (Claude Code), Copilot context, and CLI
- Auto-updates daily via Windows Task Scheduler

## Quick Start

```bash
git clone https://github.com/hihihhi/quant-research-vault
cd quant-research-vault
python install.py
```

Then run the two-phase build:

```bash
# Phase 1: Index all ~40,000 papers via abstracts (~2 hours)
python run.py --all-history --abstract-only

# Phase 2: Full Claude analysis with 3 parallel workers (runs in background, ~2-3 days)
python run.py --all-history --workers 3
```

After Phase 1 completes, the vault is already searchable. Phase 2 enriches each paper with Claude's full trading analysis (signal construction, Sharpe ratios, crypto applicability, etc.).

## Integration

### Claude Code (MCP)

Already wired by `install.py`. Claude Code can now call:

- `search_papers("momentum factor crypto")` — semantic search over vault
- `list_recent_papers(7)` — papers fetched in the last 7 days
- `get_paper("2401.12345")` — full paper analysis
- `generate_alpha_ideas("pairs trading")` — AI-generated alpha ideas from vault
- `get_vault_stats()` — total papers, date range, index size

### GitHub Copilot

```bash
python scripts/generate-copilot-context.py
```

Writes a snapshot of vault knowledge to `.github/memory/quant-brain.md`, which is injected into Copilot context via the memory system. Re-run weekly to keep it fresh.

### CLI Research

```bash
# Semantic search
python research.py "volatility forecasting"

# Generate alpha ideas via Claude (uses top 8 relevant papers as context)
python research.py --alpha-ideas "crypto momentum"

# Vault statistics
python research.py --stats

# Today's new papers
python research.py --daily

# Find papers related to a specific arXiv paper
python research.py --related 2401.12345

# Top papers by crypto applicability score (requires Phase 2)
python research.py --top-crypto 10

# Export search results to markdown
python research.py --export "momentum factor" momentum-export.md
```

## Daily Updates

A Windows Task Scheduler job runs daily at 6am:

```bash
python run.py  # fetches last 14 days + processes new papers + syncs ChromaDB
```

To run manually at any time:

```bash
python run.py
```

## Vault Structure

```
ClaudeVault/research/
├── q-fin.TR/    # Trading & Microstructure
├── q-fin.PM/    # Portfolio Management
├── q-fin.ST/    # Statistical Finance
├── q-fin.RM/    # Risk Management
├── q-fin.CP/    # Computational Finance
└── q-fin.MF/    # Mathematical Finance
```

Each paper file contains:

- Full abstract
- Signal / Alpha Idea
- Construction recipe
- Key parameters
- Empirical results (exact Sharpe, t-stats)
- Failure modes and regime conditions
- Crypto + HK Equity applicability ratings (1-5)
- Implementation checklist

## Configuration

Edit `config.yaml`:

| Key | Default | Description |
|-----|---------|-------------|
| `vault_path` | `~/Documents/ClaudeVault` | Where vault files are stored (supports `~`) |
| `claude_model` | `claude-haiku-4-5-20251001` | Model used for paper summarization |
| `days_lookback` | `14` | Days back for daily pipeline runs |
| `fetch_pdf` | `true` | Download full PDF for Phase 2 analysis |
| `max_pdf_chars` | `12000` | Max characters sent to Claude from PDF |

## Re-process a specific paper

```bash
python process.py --arxiv-id 2401.12345
```

## Pipeline scripts

| Script | Purpose |
|--------|---------|
| `fetch.py` | Pull paper metadata from arXiv API |
| `process.py` | Generate vault files (Phase 1: abstract-only, Phase 2: full Claude) |
| `sync.py` | Index vault files into ChromaDB for semantic search |
| `run.py` | Orchestrates fetch + process + sync |
| `search_mcp.py` | MCP server — exposes vault to Claude Code |
| `research.py` | Human/agent CLI for vault search and alpha generation |
| `install.py` | One-command setup: deps, dirs, DB init, MCP wiring, Task Scheduler |
| `scripts/generate-copilot-context.py` | Generate `.github/memory/quant-brain.md` for Copilot |

## Requirements

- Python >= 3.11
- `claude` CLI installed (Anthropic Max plan) — used for paper analysis and alpha generation
- Windows (for Task Scheduler integration; pipeline scripts work cross-platform)

Install dependencies:

```bash
pip install -r requirements.txt
```
