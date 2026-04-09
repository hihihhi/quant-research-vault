#!/usr/bin/env python3
"""
search_mcp.py — MCP server for semantic search over the quant research vault.

Exposes three tools:
  - search_papers(query, n_results)    semantic search over summaries
  - list_recent_papers(days)           papers added in last N days
  - get_paper(arxiv_id)                full summary for a specific paper

The ChromaDB index is built/updated by sync.py. This server is read-only.

Usage (as MCP server — Claude Code calls this automatically):
    python search_mcp.py

Usage (test):
    python search_mcp.py --test "momentum crypto"
"""

import argparse
import asyncio
import json
import sqlite3
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
