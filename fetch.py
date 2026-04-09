#!/usr/bin/env python3
"""
fetch.py — Pull new quant finance papers from arXiv.

Queries configured categories, filters ml_categories by keywords,
and stores paper metadata in SQLite. Returns only papers not yet processed.

Usage:
    python fetch.py                  # use config.yaml
    python fetch.py --config other.yaml
    python fetch.py --days 7         # override lookback days
    python fetch.py --dry-run        # print papers without saving
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import arxiv
import yaml


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id    TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            authors     TEXT NOT NULL,
            abstract    TEXT NOT NULL,
            categories  TEXT NOT NULL,
            published   TEXT NOT NULL,
            pdf_url     TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            processed   INTEGER DEFAULT 0,
            vault_path  TEXT
        )
    """)
    conn.commit()
    return conn


def already_fetched(conn: sqlite3.Connection, arxiv_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
    return row is not None


def save_paper(conn: sqlite3.Connection, paper: dict) -> None:
    conn.execute("""
        INSERT OR IGNORE INTO papers
            (arxiv_id, title, authors, abstract, categories, published, pdf_url, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        paper["arxiv_id"],
        paper["title"],
        json.dumps(paper["authors"]),
        paper["abstract"],
        json.dumps(paper["categories"]),
        paper["published"],
        paper["pdf_url"],
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()


def get_unprocessed(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT arxiv_id, title, authors, abstract, categories, published, pdf_url
        FROM papers WHERE processed = 0
        ORDER BY published DESC
    """).fetchall()
    return [
        {
            "arxiv_id": r[0],
            "title": r[1],
            "authors": json.loads(r[2]),
            "abstract": r[3],
            "categories": json.loads(r[4]),
            "published": r[5],
            "pdf_url": r[6],
        }
        for r in rows
    ]


# ── Fetch ─────────────────────────────────────────────────────────────────────

def build_query(categories: list[str]) -> str:
    parts = [f"cat:{c}" for c in categories]
    return " OR ".join(parts)


def matches_keywords(paper: arxiv.Result, keywords: list[str]) -> bool:
    text = (paper.title + " " + paper.summary).lower()
    return any(kw.lower() in text for kw in keywords)


def fetch_papers(cfg: dict, days_override: int | None = None) -> list[dict]:
    days = days_override or cfg["days_lookback"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
    results: list[dict] = []

    # Regular quant-fin categories
    if cfg.get("categories"):
        query = build_query(cfg["categories"])
        search = arxiv.Search(
            query=query,
            max_results=cfg["max_papers_per_run"],
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        for r in client.results(search):
            if r.published < cutoff:
                break
            results.append(_to_dict(r))

    # ML categories — keyword filtered
    if cfg.get("ml_categories") and cfg.get("ml_keywords"):
        query = build_query(cfg["ml_categories"])
        search = arxiv.Search(
            query=query,
            max_results=cfg["max_papers_per_run"] * 3,  # fetch more, filter down
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        for r in client.results(search):
            if r.published < cutoff:
                break
            if matches_keywords(r, cfg["ml_keywords"]):
                results.append(_to_dict(r))

    # Deduplicate by arxiv_id
    seen: set[str] = set()
    unique = []
    for p in results:
        if p["arxiv_id"] not in seen:
            seen.add(p["arxiv_id"])
            unique.append(p)

    return unique


def _to_dict(r: arxiv.Result) -> dict:
    return {
        "arxiv_id": r.entry_id.split("/abs/")[-1],
        "title": r.title.strip(),
        "authors": [a.name for a in r.authors[:5]],  # cap at 5
        "abstract": r.summary.strip(),
        "categories": r.categories,
        "published": r.published.isoformat(),
        "pdf_url": r.pdf_url,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch quant finance papers from arXiv")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, help="Override days_lookback")
    parser.add_argument("--dry-run", action="store_true", help="Print without saving")
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = init_db(cfg["db_path"])

    print(f"Fetching papers (last {args.days or cfg['days_lookback']} days)...")
    papers = fetch_papers(cfg, days_override=args.days)
    print(f"Found {len(papers)} papers from arXiv")

    new_count = 0
    for p in papers:
        if already_fetched(conn, p["arxiv_id"]):
            continue
        if args.dry_run:
            print(f"  [DRY] {p['arxiv_id']}: {p['title'][:80]}")
        else:
            save_paper(conn, p)
            new_count += 1
            print(f"  + {p['arxiv_id']}: {p['title'][:70]}")

    if not args.dry_run:
        pending = get_unprocessed(conn)
        print(f"\nSaved {new_count} new papers. {len(pending)} total pending processing.")

    conn.close()


if __name__ == "__main__":
    main()
