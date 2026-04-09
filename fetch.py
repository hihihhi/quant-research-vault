#!/usr/bin/env python3
"""
fetch.py — Pull quant finance papers from arXiv.

Usage:
    python fetch.py                         # use config.yaml (14-day window)
    python fetch.py --days 7                # override lookback days
    python fetch.py --all-history           # fetch ALL papers (date-range chunks)
    python fetch.py --window-start 20200101 --window-end 20200401  # specific window
    python fetch.py --dry-run               # print papers without saving
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import arxiv
import yaml

# Windows terminals may use cp950/cp1252 — force UTF-8 output
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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


def count_total(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]


# ── Date window generation ────────────────────────────────────────────────────

def date_windows(start: date, end: date, chunk_months: int = 3) -> list[tuple[date, date]]:
    """Split [start, end] into chunks of ~chunk_months months."""
    windows = []
    cur = start
    while cur < end:
        month = cur.month - 1 + chunk_months
        year = cur.year + month // 12
        month = month % 12 + 1
        nxt = date(year, month, 1)
        if nxt > end:
            nxt = end
        windows.append((cur, nxt))
        cur = nxt
    return windows


# ── Fetch ─────────────────────────────────────────────────────────────────────

def build_query(categories: list[str], window_start: date | None = None, window_end: date | None = None) -> str:
    cat_part = " OR ".join(f"cat:{c}" for c in categories)
    if window_start and window_end:
        d_start = window_start.strftime("%Y%m%d")
        d_end = window_end.strftime("%Y%m%d")
        return f"({cat_part}) AND submittedDate:[{d_start} TO {d_end}]"
    return cat_part


def matches_keywords(paper: arxiv.Result, keywords: list[str]) -> bool:
    text = (paper.title + " " + paper.summary).lower()
    return any(kw.lower() in text for kw in keywords)


def _to_dict(r: arxiv.Result) -> dict:
    return {
        "arxiv_id": r.entry_id.split("/abs/")[-1],
        "title": r.title.strip(),
        "authors": [a.name for a in r.authors[:5]],
        "abstract": r.summary.strip(),
        "categories": r.categories,
        "published": r.published.isoformat(),
        "pdf_url": r.pdf_url,
    }


def _iter_with_retry(client: arxiv.Client, search: arxiv.Search, max_retries: int = 4) -> list[arxiv.Result]:
    """Iterate results with retry + backoff on HTTP 429/500."""
    import time
    for attempt in range(max_retries):
        try:
            return list(client.results(search))
        except arxiv.HTTPError as e:
            if e.status == 429:
                wait = 60 * (2 ** attempt)  # 60s, 120s, 240s, 480s
                print(f"  Rate limited (429). Waiting {wait}s before retry {attempt+1}/{max_retries}...", flush=True)
                time.sleep(wait)
            elif e.status == 500:
                wait = 30 * (attempt + 1)
                print(f"  Server error (500). Waiting {wait}s before retry {attempt+1}/{max_retries}...", flush=True)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Giving up after {max_retries} retries")


def fetch_window(cfg: dict, window_start: date | None = None, window_end: date | None = None,
                 max_per_query: int = 2000) -> list[dict]:
    """Fetch papers in a specific date window (or all if no window given)."""
    client = arxiv.Client(page_size=100, delay_seconds=5, num_retries=3)
    results: list[dict] = []
    seen: set[str] = set()

    # Regular quant-fin categories
    if cfg.get("categories"):
        query = build_query(cfg["categories"], window_start, window_end)
        search = arxiv.Search(
            query=query,
            max_results=max_per_query,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        for r in _iter_with_retry(client, search):
            pid = r.entry_id.split("/abs/")[-1]
            if pid not in seen:
                seen.add(pid)
                results.append(_to_dict(r))
                if len(results) % 100 == 0:
                    print(f"  [q-fin] {len(results)} papers...", flush=True)

    # ML categories — keyword filtered
    if cfg.get("ml_categories") and cfg.get("ml_keywords"):
        import time
        time.sleep(5)  # brief pause between category queries
        ml_count = 0
        query = build_query(cfg["ml_categories"], window_start, window_end)
        search = arxiv.Search(
            query=query,
            max_results=max_per_query,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        for r in _iter_with_retry(client, search):
            pid = r.entry_id.split("/abs/")[-1]
            if pid not in seen and matches_keywords(r, cfg["ml_keywords"]):
                seen.add(pid)
                results.append(_to_dict(r))
                ml_count += 1
                if ml_count % 100 == 0:
                    print(f"  [ml] {ml_count} relevant ML papers...", flush=True)

    return results


def fetch_recent(cfg: dict, days_override: int | None = None) -> list[dict]:
    """Fetch papers from the last N days (normal mode)."""
    days = days_override or cfg["days_lookback"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    client = arxiv.Client(page_size=200, delay_seconds=3, num_retries=5)
    results: list[dict] = []
    seen: set[str] = set()

    if cfg.get("categories"):
        query = " OR ".join(f"cat:{c}" for c in cfg["categories"])
        search = arxiv.Search(
            query=query,
            max_results=cfg["max_papers_per_run"],
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        for r in client.results(search):
            if r.published < cutoff:
                break
            pid = r.entry_id.split("/abs/")[-1]
            if pid not in seen:
                seen.add(pid)
                results.append(_to_dict(r))

    if cfg.get("ml_categories") and cfg.get("ml_keywords"):
        query = " OR ".join(f"cat:{c}" for c in cfg["ml_categories"])
        search = arxiv.Search(
            query=query,
            max_results=cfg["max_papers_per_run"] * 3,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        for r in client.results(search):
            if r.published < cutoff:
                break
            pid = r.entry_id.split("/abs/")[-1]
            if pid not in seen and matches_keywords(r, cfg["ml_keywords"]):
                seen.add(pid)
                results.append(_to_dict(r))

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch quant finance papers from arXiv")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, help="Override days_lookback")
    parser.add_argument("--all-history", action="store_true", help="Fetch ALL papers via date-range chunks")
    parser.add_argument("--window-start", help="Specific window start YYYYMMDD")
    parser.add_argument("--window-end", help="Specific window end YYYYMMDD")
    parser.add_argument("--dry-run", action="store_true", help="Print without saving")
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = init_db(cfg["db_path"])

    if args.all_history:
        # Date-range chunked fetch — bypasses arXiv's 10k offset limit
        # q-fin categories started around 1997; start from 1993 to be safe
        start_date = date(1993, 1, 1)
        end_date = date.today()
        windows = date_windows(start_date, end_date, chunk_months=3)
        total_new = 0

        print(f"All-history mode: {len(windows)} quarterly windows from {start_date} to {end_date}")

        for i, (w_start, w_end) in enumerate(windows, 1):
            print(f"\n[{i}/{len(windows)}] Window: {w_start} -> {w_end}", flush=True)
            papers = fetch_window(cfg, window_start=w_start, window_end=w_end)
            new_count = 0
            for p in papers:
                if not already_fetched(conn, p["arxiv_id"]):
                    if not args.dry_run:
                        save_paper(conn, p)
                    new_count += 1
            total_new += new_count
            total_db = count_total(conn)
            print(f"  Window done: {len(papers)} found, {new_count} new. DB total: {total_db}", flush=True)

        print(f"\nAll-history fetch complete. Total new papers: {total_new}. DB total: {count_total(conn)}")
        conn.close()
        return 0

    if args.window_start and args.window_end:
        w_start = datetime.strptime(args.window_start, "%Y%m%d").date()
        w_end = datetime.strptime(args.window_end, "%Y%m%d").date()
        papers = fetch_window(cfg, window_start=w_start, window_end=w_end)
        label = f"window {w_start} -> {w_end}"
    else:
        days = args.days or cfg["days_lookback"]
        print(f"Fetching papers (last {days} days)...")
        papers = fetch_recent(cfg, days_override=args.days)
        label = f"last {days} days"

    print(f"Found {len(papers)} papers from arXiv ({label})")

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
