# Quant Research Vault: Local Paper Ingestion and Semantic Search over MCP

[![quality](https://github.com/oscar-chw/quant-research-vault/actions/workflows/quality.yml/badge.svg)](https://github.com/oscar-chw/quant-research-vault/actions/workflows/quality.yml)

A local Python pipeline that fetches academic-paper metadata from arXiv (optionally OpenAlex) into SQLite, indexes processed records in ChromaDB, and exposes read-only semantic search through an MCP server. At a 2026-07-30 audit its local, unpublished database held 18,492 paper rows ([query and SHA-256](docs/corpus_audit_snapshot_2026-07-30.json)).

From upstream metadata to an AI assistant's search: deduplicated into SQLite, written to the vault, indexed in
ChromaDB, and served read-only over MCP by a single server instance.

```mermaid
flowchart TB
    subgraph up["upstream APIs"]
        ARX["arXiv<br/>configured categories"]
        OA["OpenAlex<br/>windowed fetch only"]
    end
    FETCH["fetch.py<br/>429/500 backoff in windows"]
    DD{"arxiv_id already<br/>seen or stored?"}
    DB[("SQLite papers<br/>arxiv_id primary key")]
    PROC["process.py<br/>abstract or full entry"]
    VAULT[("vault markdown<br/>one file per paper")]
    SYNC["sync.py<br/>skips indexed IDs"]
    CH[("ChromaDB<br/>quant_papers, cosine")]
    START["python search_mcp.py<br/>started by the client"]
    LOCK{"PID lock file:<br/>live instance?"}
    EXIT["exit 0<br/>client does not retry"]
    MCP["search_mcp.py<br/>5 read-only tools"]
    AI["AI assistant<br/>MCP client"]

    ARX -- "paper metadata" --> FETCH
    OA -. "non-arXiv works" .-> FETCH
    FETCH == "records" ==> DD
    DD -- "yes: skipped" --> FETCH
    DD == "no: INSERT OR IGNORE" ==> DB
    DB -- "processed = 0" --> PROC
    PROC -- "writes .md" --> VAULT
    PROC -- "processed = 1,<br/>vault_path" --> DB
    DB == "processed rows" ==> SYNC
    VAULT -- "document text" --> SYNC
    SYNC == "upsert by arxiv_id" ==> CH
    START -- "on startup" --> LOCK
    LOCK -- "yes" --> EXIT
    LOCK == "no: write own PID" ==> MCP
    CH == "query results" ==> MCP
    DB -- "recent papers" --> MCP
    MCP == "text over stdio" ==> AI

    classDef data fill:#dbeafe,stroke:#1d4ed8,color:#0b1220
    classDef step fill:#f1f5f9,stroke:#475569,color:#0b1220
    classDef gate fill:#fef3c7,stroke:#b45309,color:#0b1220
    classDef ext  fill:#f8fafc,stroke:#94a3b8,color:#0b1220,stroke-dasharray:4 3
    classDef key  fill:#ede9fe,stroke:#6d28d9,color:#0b1220,stroke-width:2px
    class DB,VAULT,CH data
    class PROC,START step
    class DD,LOCK,EXIT gate
    class ARX,OA,AI ext
    class FETCH,SYNC,MCP key
```

Where in the code: `fetch.py` (`fetch_recent`, `fetch_window`, `_iter_with_retry`, `fetch_openalex_window`,
`already_fetched`, `save_paper`), `process.py` (`get_pending`, `mark_processed`), `sync.py` (`already_indexed`,
`index_paper`), `search_mcp.py` (`_acquire_lock`, `build_server`), `config.yaml`.

**Status:** arXiv-ID deduplication, non-overlapping fetch date windows, rate-limit backoff, the single-instance MCP lock and search-result mapping pass 5 offline tests; CI runs ruff, mypy and pytest.

Run the checks with `pytest -q` and see the MCP server's options with `python search_mcp.py --help`; PowerShell and bash steps are in [Run it](#run-it).

Implemented with AI coding agents under Oscar's design and review.

## Architecture

- `fetch.py` queries configured arXiv categories and optional OpenAlex; Semantic Scholar is configured but disabled by default. Records are persisted in SQLite using `INSERT OR IGNORE` keyed by paper ID.
- In the windowed history fetch (`--all-history`, `fetch_window`), arXiv requests make up to 4 attempts. After an HTTP 429 the waits are 60, 120 and 240 seconds before the retries (and 480 seconds before giving up); after an HTTP 500 they are 30, 60 and 90 seconds (and 120 before giving up).
- `process.py` optionally enriches local records; `sync.py` indexes processed records in ChromaDB and skips IDs already present.
- `search_mcp.py` provides read-only semantic search and stats over ChromaDB/SQLite, with a temporary PID lock to reject another live MCP instance and clear stale locks.
- `run.py` orchestrates fetch -> process -> sync; `master.py` coordinates longer, restartable source and distillation stages.

One `search_papers` call, from server start to the text the assistant reads:

```mermaid
sequenceDiagram
    participant AI as AI assistant
    participant S as search_mcp.py
    participant L as lock file
    participant C as ChromaDB
    AI->>S: start the server over stdio
    S->>L: _acquire_lock reads the PID
    alt that PID is alive
        S-->>AI: exit 0, so not retried
    else no file or a stale PID
        S->>L: write own PID
        S->>C: get_collection quant_papers
    end
    AI->>S: call_tool search_papers, query, n_results
    S->>S: n = min(n_results, 10)
    S->>C: query, query_texts and n
    C-->>S: documents, metadatas, distances
    S->>S: score 1 - distance, 500-char excerpt
    S-->>AI: text with title, id, excerpt
    Note over S,L: on exit _release_lock deletes it
```

Where in the code: `search_mcp.py` (`_acquire_lock`, `_release_lock`, `get_collection`, `search_papers`,
`call_tool` in `build_server`); the lock and result mapping are covered by `test_quality.py`.

The daily job: `install.py` schedules `run.py` at 06:00, which runs three child processes in order and stops at the
first that fails. The daily fetch is the last 14 days through the arXiv client's own retries; the 429/500 backoff
above applies to the windowed history fetch (`run.py --all-history`).

```mermaid
flowchart TB
    SCHED["install.py daily job<br/>06:00, schtasks or cron"]
    RUN["run.py<br/>each step a child<br/>process with a time cap"]
    F["fetch.py, 30 min<br/>last days_lookback = 14"]
    API["arXiv client<br/>delay 3 s, 5 retries"]
    FILT["per profile: categories,<br/>keywords, seen set"]
    NEW{"already_fetched?"}
    DB[("SQLite papers")]
    P["process.py, 2 h<br/>pending rows"]
    KEY{"ANTHROPIC_API_KEY<br/>set?"}
    MD[("vault .md<br/>processed = 1")]
    S["sync.py, 15 min"]
    CH[("ChromaDB")]
    STOP["exit: later steps<br/>do not run"]

    SCHED == "starts" ==> RUN
    RUN == "step 1" ==> F
    F -- "query, newest first" --> API
    API -- "results until cutoff" --> FILT
    FILT == "new IDs" ==> NEW
    NEW -- "yes: skip" --> FILT
    NEW == "no: save_paper" ==> DB
    F -- "exit not 0" --> STOP
    F == "exit 0: step 2" ==> P
    DB -- "processed = 0" --> P
    P -- "full mode" --> KEY
    KEY == "yes: PDF and<br/>summary written" ==> MD
    KEY -- "no: error,<br/>row stays pending" --> DB
    P -- "exit not 0" --> STOP
    P == "exit 0: step 3" ==> S
    MD -- "processed rows" --> S
    S == "upsert new IDs" ==> CH

    classDef data fill:#dbeafe,stroke:#1d4ed8,color:#0b1220
    classDef step fill:#f1f5f9,stroke:#475569,color:#0b1220
    classDef gate fill:#fef3c7,stroke:#b45309,color:#0b1220
    classDef ext  fill:#f8fafc,stroke:#94a3b8,color:#0b1220,stroke-dasharray:4 3
    classDef key  fill:#ede9fe,stroke:#6d28d9,color:#0b1220,stroke-width:2px
    class DB,MD,CH data
    class FILT step
    class NEW,KEY,STOP gate
    class SCHED,API ext
    class RUN,F,P,S key
```

Where in the code: `install.py` (`create_scheduled_task`), `run.py` (`run_step`, `_STEP_TIMEOUT`, `main`),
`fetch.py` (`fetch_recent`, `already_fetched`, `save_paper`), `process.py` (`_process_one`, `summarize`),
`sync.py` (`main`), `config.yaml` (`days_lookback`).

## The interesting decision

The project decouples abstract-only indexing from optional full-text/model-assisted enrichment. This makes a locally retrieved corpus searchable before the slower enrichment stage; the trade-off is that early retrieval quality is limited to metadata and abstracts, while later enrichment requires local files and optional model tooling.

## Provenance

- arXiv and OpenAlex are the configured upstream metadata sources (`config.yaml`, `fetch.py`); their availability, coverage, licenses, and API limits remain upstream concerns.
- SQLite is the local state store and ChromaDB is the local vector index (`fetch.py`, `sync.py`, `search_mcp.py`).
- The MCP server is local and read-only with respect to the retrieval interface (`search_mcp.py`).
- `docs/corpus_audit_snapshot_2026-07-30.json` records `SELECT count(*) FROM papers = 18492` against an ignored 33,161,216-byte SQLite file with SHA-256 `acb1a76ee2bf575fa083c807be39b66b34c040a6b9697a297b1e0dc0a0e7ab13`; the underlying rows are not published.
- Implemented with AI coding agents (OpenAI Codex, Claude Code) under Oscar Choi's design and review.
- Research-infrastructure project; last functional change 2026-04-12.
- Repository license: MIT (see `LICENSE`).

## Run it

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt ruff mypy pytest
& .\.venv\Scripts\ruff.exe check .
& .\.venv\Scripts\ruff.exe format --check .
& .\.venv\Scripts\mypy.exe
& .\.venv\Scripts\pytest.exe -q
& .\.venv\Scripts\python.exe run.py --fetch-only --dry-run
& .\.venv\Scripts\python.exe search_mcp.py --help
```

```bash
# macOS/Linux (bash): the same steps
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt ruff mypy pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest -q
.venv/bin/python run.py --fetch-only --dry-run
.venv/bin/python search_mcp.py --help
```

The dry run makes live upstream requests but is intended not to persist fetched records. Run `python run.py --help` before any stateful ingestion command.

## Limitations

- The 18,492-row audit-time count is an operational row count, not a quality or model-performance result, and its sample status is unknown; the code's `processed` flag means an entry was written, not that analysis is complete. No trading, predictive, retrieval-quality, or benchmark result is claimed.
- The committed audit snapshot preserves a local row count and source hash, but the corpus, SQLite database, and ChromaDB index are not published; the count, coverage, and retrieval quality are therefore not independently reproducible from this checkout.
- Upstream API schema, rate-limit, and availability changes can affect ingestion.
- The PID lock is a local single-instance guard, not a distributed lock.
- Optional enrichment depends on local files and model/tool configuration; it is not exercised by the clean-clone quality suite.
