#!/usr/bin/env python3
"""
search_mcp.py — MCP server for semantic search over the quant research vault.

Exposes five tools:
  - search_papers(query, n_results)         semantic search over summaries
  - list_recent_papers(days)                papers added in last N days
  - get_paper(arxiv_id)                     full summary for a specific paper
  - generate_alpha_ideas(topic, n_papers)   AI-generated alpha ideas from vault
  - get_vault_stats()                       total papers, date range, index size

The ChromaDB index is built/updated by sync.py. This server is read-only.

Usage (as MCP server — Claude Code calls this automatically):
    python search_mcp.py

Usage (test):
    python search_mcp.py --test "momentum crypto"
"""

import argparse
import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import chromadb
import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types


CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_collection(cfg: dict) -> chromadb.Collection:
    client = chromadb.PersistentClient(path=cfg["chroma_path"])
    return client.get_or_create_collection(
        name="quant_papers",
        metadata={"hnsw:space": "cosine"},
    )


def get_db(cfg: dict) -> sqlite3.Connection:
    return sqlite3.connect(cfg["db_path"])


# ── Tool implementations ──────────────────────────────────────────────────────

def search_papers(collection: chromadb.Collection, query: str, n_results: int = 5) -> list[dict]:
    try:
        results = collection.query(
            query_texts=[query],
            n_results=min(n_results, collection.count() or 1),
            include=["documents", "metadatas", "distances"],
        )
    except Exception:
        return []

    papers = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        papers.append({
            "arxiv_id": meta.get("arxiv_id"),
            "title": meta.get("title"),
            "categories": meta.get("categories"),
            "published": meta.get("published"),
            "relevance_score": round(1 - dist, 3),
            "vault_path": meta.get("vault_path"),
            "summary_excerpt": doc[:500] + "..." if len(doc) > 500 else doc,
        })
    return papers


def list_recent_papers(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT arxiv_id, title, categories, published, vault_path
        FROM papers
        WHERE processed = 1 AND fetched_at > ?
        ORDER BY published DESC
        LIMIT 20
    """, (cutoff,)).fetchall()
    return [
        {
            "arxiv_id": r[0],
            "title": r[1],
            "categories": json.loads(r[2]) if r[2] else [],
            "published": r[3][:10],
            "vault_path": r[4],
        }
        for r in rows
    ]


def get_paper_summary(cfg: dict, arxiv_id: str) -> str | None:
    conn = get_db(cfg)
    row = conn.execute(
        "SELECT vault_path FROM papers WHERE arxiv_id = ? AND processed = 1",
        (arxiv_id,)
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    path = Path(row[0])
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def generate_alpha_ideas_text(
    collection: chromadb.Collection,
    cfg: dict,
    topic: str,
    n_papers: int = 6,
) -> str:
    """Search vault for relevant papers and ask Claude for alpha ideas."""
    count = collection.count()
    if count == 0:
        return "ChromaDB index is empty. Run `python run.py` to build it first."

    results = collection.query(
        query_texts=[topic],
        n_results=min(n_papers, count),
        include=["documents", "metadatas"],
    )

    paper_summaries: list[str] = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        arxiv_id = meta.get("arxiv_id", "")
        title = meta.get("title", "")
        vault_path = meta.get("vault_path", "")
        content = doc[:2000]

        if vault_path:
            vp = Path(vault_path)
            if vp.exists():
                content = vp.read_text(encoding="utf-8")[:2000]

        paper_summaries.append(
            f"--- Paper: {title} ({arxiv_id}) ---\n{content}"
        )

    model = cfg.get("claude_model", "claude-haiku-4-5-20251001")
    prompt = (
        f"You are a quant researcher specializing in systematic trading strategies.\n"
        f"Based on the following {len(paper_summaries)} research papers, generate 3 specific, "
        f"implementable alpha ideas for the topic: '{topic}'.\n\n"
        f"For each idea include:\n"
        f"1. Signal construction (exact steps)\n"
        f"2. Data required (source, frequency)\n"
        f"3. Expected holding period\n"
        f"4. Key risk and failure mode\n\n"
        + "\n\n".join(paper_summaries)
    )

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return (
            "ERROR: 'claude' CLI not found. "
            "Install it via: npm install -g @anthropic-ai/claude-code"
        )

    try:
        result = subprocess.run(
            [claude_bin, "--output-format", "text", "--model", model, "-p", "-"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "Claude CLI timed out after 300 seconds."

    if result.returncode != 0:
        return f"Claude CLI error: {result.stderr[:300]}"

    return result.stdout.strip()


def get_vault_stats_text(cfg: dict, collection: chromadb.Collection) -> str:
    """Return vault statistics as a formatted string."""
    conn = get_db(cfg)
    total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    processed = conn.execute(
        "SELECT COUNT(*) FROM papers WHERE processed = 1"
    ).fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(published), MAX(published) FROM papers"
    ).fetchone()
    conn.close()

    min_date = (date_range[0] or "")[:10]
    max_date = (date_range[1] or "")[:10]
    index_size = collection.count()

    return (
        f"Quant Research Vault Statistics\n"
        f"================================\n"
        f"Total papers in DB    : {total:,}\n"
        f"Processed papers      : {processed:,}\n"
        f"ChromaDB index size   : {index_size:,}\n"
        f"Date range            : {min_date} to {max_date}\n"
    )


# ── MCP server ────────────────────────────────────────────────────────────────

def build_server() -> Server:
    cfg = load_config()
    collection = get_collection(cfg)
    conn = get_db(cfg)

    server = Server("quant-research")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="search_papers",
                description=(
                    "Semantic search over quant finance research papers in the vault. "
                    "Returns the most relevant papers for a given query. "
                    "Use for: finding papers on a specific alpha factor, technique, or market, "
                    "before engineering features or designing strategies."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language search query, e.g. 'momentum factor in crypto', 'volatility prediction LSTM'",
                        },
                        "n_results": {
                            "type": "integer",
                            "description": "Number of results to return (default: 5, max: 10)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="list_recent_papers",
                description="List quant finance papers added to the vault in the last N days.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "How many days back to look (default: 7)",
                            "default": 7,
                        },
                    },
                },
            ),
            types.Tool(
                name="get_paper",
                description="Get the full structured summary for a specific paper by arXiv ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "arxiv_id": {
                            "type": "string",
                            "description": "arXiv paper ID, e.g. '2601.12345' or '2601.12345v2'",
                        },
                    },
                    "required": ["arxiv_id"],
                },
            ),
            types.Tool(
                name="generate_alpha_ideas",
                description=(
                    "Search the vault for relevant research papers and generate specific, "
                    "implementable alpha trading ideas on a given topic using Claude AI. "
                    "Returns 3 alpha ideas with signal construction, data requirements, "
                    "holding period, and key risks."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "The trading topic to generate ideas for, e.g. 'crypto momentum', 'volatility arbitrage'",
                        },
                        "n_papers": {
                            "type": "integer",
                            "description": "Number of vault papers to use as context (default: 6, max: 10)",
                            "default": 6,
                        },
                    },
                    "required": ["topic"],
                },
            ),
            types.Tool(
                name="get_vault_stats",
                description=(
                    "Get statistics about the quant research vault: total papers, "
                    "processed count, date range covered, and ChromaDB index size."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        if name == "search_papers":
            query = arguments["query"]
            n = min(int(arguments.get("n_results", 5)), 10)
            results = search_papers(collection, query, n)
            if not results:
                text = f"No papers found matching '{query}'. Run `python run.py` to fetch and index papers first."
            else:
                lines = [f"Found {len(results)} papers matching '{query}':\n"]
                for r in results:
                    lines.append(
                        f"**[{r['relevance_score']:.2f}] {r['title']}**\n"
                        f"arXiv: {r['arxiv_id']} | {r['published'][:10]} | {r['categories']}\n"
                        f"{r['summary_excerpt']}\n"
                    )
                text = "\n---\n".join(lines)

        elif name == "list_recent_papers":
            days = int(arguments.get("days", 7))
            papers = list_recent_papers(conn, days)
            if not papers:
                text = f"No papers processed in the last {days} days."
            else:
                lines = [f"{len(papers)} papers added in the last {days} days:\n"]
                for p in papers:
                    lines.append(f"- **{p['title']}** ({p['arxiv_id']}) — {p['published']}")
                text = "\n".join(lines)

        elif name == "get_paper":
            arxiv_id = arguments["arxiv_id"]
            summary = get_paper_summary(cfg, arxiv_id)
            text = summary if summary else f"Paper {arxiv_id} not found or not yet processed."

        elif name == "generate_alpha_ideas":
            topic = arguments["topic"]
            n_papers = min(int(arguments.get("n_papers", 6)), 10)
            text = generate_alpha_ideas_text(collection, cfg, topic, n_papers)

        elif name == "get_vault_stats":
            text = get_vault_stats_text(cfg, collection)

        else:
            text = f"Unknown tool: {name}"

        return [types.TextContent(type="text", text=text)]

    return server


async def run_server() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# ── CLI test mode ─────────────────────────────────────────────────────────────

def test_search(query: str) -> None:
    cfg = load_config()
    collection = get_collection(cfg)
    results = search_papers(collection, query)
    if not results:
        print("No results. Have you run `python run.py` yet?")
        return
    for r in results:
        print(f"\n[{r['relevance_score']:.2f}] {r['title']}")
        print(f"  arXiv: {r['arxiv_id']} | {r['published'][:10]}")
        print(f"  {r['summary_excerpt'][:200]}...")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", metavar="QUERY", help="Test search without MCP")
    args = parser.parse_args()

    if args.test:
        test_search(args.test)
    else:
        asyncio.run(run_server())
