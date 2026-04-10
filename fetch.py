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
import requests
import yaml

# Windows terminals may use cp950/cp1252 — force UTF-8 output
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["vault_path"] = str(Path(cfg["vault_path"]).expanduser())
    return cfg


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


def _invert_abstract(inv_index: dict) -> str:
    """Reconstruct abstract from OpenAlex inverted index {word: [positions]}."""
    if not inv_index:
        return ""
    pairs = []
    for word, positions in inv_index.items():
        for pos in positions:
            pairs.append((pos, word))
    pairs.sort()
    return " ".join(w for _, w in pairs)


def _openalex_to_cat(concepts: list) -> str:
    """Map OpenAlex top concept to q-fin category."""
    name = (concepts[0].get("display_name", "") if concepts else "").lower()
    if any(x in name for x in ("portfolio", "asset pricing", "fund")):
        return "q-fin.PM"
    if any(x in name for x in ("trading", "microstructure", "market making", "order")):
        return "q-fin.TR"
    if any(x in name for x in ("risk",)):
        return "q-fin.RM"
    if any(x in name for x in ("stochastic", "volatility", "statistical")):
        return "q-fin.ST"
    if any(x in name for x in ("computational", "numerical", "simulation")):
        return "q-fin.CP"
    return "q-fin.GN"


def fetch_openalex_window(cfg: dict, window_start=None, window_end=None) -> list[dict]:
    """Fetch non-arXiv papers from OpenAlex (covers SSRN, journals, working papers)."""
    oa_cfg = (cfg.get("extra_sources") or {}).get("openalex", {})
    if not oa_cfg.get("enabled", False):
        return []

    import time as _time

    mailto = oa_cfg.get("mailto", "")
    max_results = oa_cfg.get("max_per_window", 300)

    # Build filter
    filters = ["has_abstract:true", "is_paratext:false", "type:article"]
    if window_start:
        filters.append(f"from_publication_date:{window_start.isoformat()}")
    if window_end:
        filters.append(f"to_publication_date:{window_end.isoformat()}")

    # Finance + quant keywords search
    search_terms = " ".join(cfg.get("ml_keywords", [
        "portfolio", "trading", "volatility", "momentum", "alpha", "cryptocurrency",
    ])[:12])

    params = {
        "search": search_terms,
        "filter": ",".join(filters),
        "select": "id,title,abstract_inverted_index,authorships,publication_date,ids,open_access,primary_location,concepts",
        "per-page": 200,
        "cursor": "*",
        "sort": "publication_date:desc",
    }
    if mailto:
        params["mailto"] = mailto

    ua = "quant-research-vault/1.0 (mailto:{}) OpenAlex".format(mailto or "anonymous")
    results = []
    seen_ids: set[str] = set()

    while len(results) < max_results:
        try:
            r = requests.get(
                "https://api.openalex.org/works",
                params=params,
                headers={"User-Agent": ua},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  [openalex] error: {e}", flush=True)
            break

        works = data.get("results") or []
        if not works:
            break

        for work in works:
            # Skip if paper has an arXiv ID — arXiv fetch already covers it
            ext_ids = work.get("ids") or {}
            if ext_ids.get("arxiv"):
                continue

            # Build synthetic ID from OpenAlex work ID
            oa_id = (work.get("id") or "").split("works/")[-1]
            if not oa_id:
                continue
            paper_id = f"oa:{oa_id}"
            if paper_id in seen_ids:
                continue
            seen_ids.add(paper_id)

            abstract = _invert_abstract(work.get("abstract_inverted_index") or {})
            if not abstract or len(abstract) < 80:
                continue

            title = (work.get("title") or "").strip()
            if not title:
                continue

            authors = [
                (a.get("author") or {}).get("display_name", "")
                for a in (work.get("authorships") or [])[:5]
            ]
            authors = [a for a in authors if a]

            concepts = work.get("concepts") or []
            categories = [_openalex_to_cat(concepts)]

            oa = work.get("open_access") or {}
            pdf_url = oa.get("oa_url") or ""
            if not pdf_url:
                loc = work.get("primary_location") or {}
                pdf_url = loc.get("pdf_url") or loc.get("landing_page_url") or ""

            pub_date = (work.get("publication_date") or "2000-01-01")[:10]

            results.append({
                "arxiv_id": paper_id,
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "categories": categories,
                "published": pub_date + "T00:00:00+00:00",
                "pdf_url": pdf_url,
            })

            if len(results) >= max_results:
                break

        next_cursor = (data.get("meta") or {}).get("next_cursor")
        if not next_cursor:
            break
        params["cursor"] = next_cursor
        _time.sleep(0.5)

    return results


def fetch_semantic_scholar_bulk(cfg: dict) -> list[dict]:
    """One-time bulk import from Semantic Scholar (use --fetch-ss flag).
    Fetches finance papers not on arXiv. Slower — use once, not in windowed loop.
    """
    ss_cfg = (cfg.get("extra_sources") or {}).get("semantic_scholar", {})
    if not ss_cfg.get("enabled", False):
        print("[semantic_scholar] disabled in config (set extra_sources.semantic_scholar.enabled: true)")
        return []

    import time as _time

    api_key = ss_cfg.get("api_key", "")
    query = ss_cfg.get("query", "quantitative finance trading portfolio")
    max_results = ss_cfg.get("max_results", 5000)
    sleep_secs = 1.1 if api_key else 3.1  # free tier: 100 req/5min

    headers = {"User-Agent": "quant-research-vault/1.0"}
    if api_key:
        headers["x-api-key"] = api_key

    fields = "paperId,title,abstract,authors,year,publicationDate,externalIds,openAccessPdf,fieldsOfStudy,s2FieldsOfStudy"

    results = []
    seen_ids: set[str] = set()
    offset = 0
    limit = 100

    print(f"[semantic_scholar] Fetching up to {max_results} finance papers (non-arXiv only)...", flush=True)

    while len(results) < max_results:
        try:
            r = requests.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": query, "fields": fields, "limit": limit, "offset": offset},
                headers=headers,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  [semantic_scholar] error at offset {offset}: {e}", flush=True)
            _time.sleep(10)
            break

        papers = data.get("data") or []
        if not papers:
            break

        for p in papers:
            ext = p.get("externalIds") or {}
            # Skip if arXiv ID exists — arXiv fetch covers it
            if ext.get("ArXiv"):
                continue

            ss_id = p.get("paperId", "")
            if not ss_id:
                continue
            paper_id = f"ss:{ss_id}"
            if paper_id in seen_ids:
                continue
            seen_ids.add(paper_id)

            abstract = (p.get("abstract") or "").strip()
            if not abstract or len(abstract) < 80:
                continue

            title = (p.get("title") or "").strip()
            if not title:
                continue

            authors = [(a.get("name") or "") for a in (p.get("authors") or [])[:5]]
            authors = [a for a in authors if a]

            fos = [f.get("category", "") for f in (p.get("s2FieldsOfStudy") or [])]
            if not any(f in ("Economics", "Finance", "Business") for f in fos):
                continue  # filter to finance-adjacent papers only

            oa = p.get("openAccessPdf") or {}
            pdf_url = oa.get("url", "")

            pub_date = p.get("publicationDate") or f"{p.get('year', 2000)}-01-01"
            if len(pub_date) == 4:
                pub_date += "-01-01"

            results.append({
                "arxiv_id": paper_id,
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "categories": ["q-fin.GN"],
                "published": pub_date[:10] + "T00:00:00+00:00",
                "pdf_url": pdf_url,
            })

            if len(results) % 500 == 0:
                print(f"  [semantic_scholar] {len(results)} papers so far...", flush=True)

            if len(results) >= max_results:
                break

        offset += limit
        total_available = data.get("total", 0)
        if offset >= min(total_available, max_results * 3):
            break
        _time.sleep(sleep_secs)

    print(f"  [semantic_scholar] Done: {len(results)} non-arXiv papers found.", flush=True)
    return results


def _active_profiles(cfg: dict) -> list[tuple[str, dict]]:
    """Return list of (name, profile) for all enabled profiles."""
    return [
        (name, p) for name, p in cfg.get("profiles", {}).items()
        if p.get("enabled")
    ]


def fetch_window(cfg: dict, window_start: date | None = None, window_end: date | None = None,
                 max_per_query: int = 2000) -> list[dict]:
    """Fetch papers in a specific date window (or all if no window given)."""
    import time as _time_mod
    client = arxiv.Client(page_size=100, delay_seconds=5, num_retries=3)
    results: list[dict] = []
    seen: set[str] = set()

    for profile_name, profile in _active_profiles(cfg):
        cats = profile.get("categories", [])
        keywords = profile.get("keywords", [])
        if not cats:
            continue
        _time_mod.sleep(3)
        query = build_query(cats, window_start, window_end)
        search = arxiv.Search(
            query=query,
            max_results=max_per_query,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        count = 0
        for r in _iter_with_retry(client, search):
            pid = r.entry_id.split("/abs/")[-1]
            if pid not in seen:
                if not keywords or matches_keywords(r, keywords):
                    seen.add(pid)
                    results.append(_to_dict(r))
                    count += 1
                    if count % 100 == 0:
                        print(f"  [{profile_name}] {count} papers...", flush=True)

    # ── Extra sources (OpenAlex/SSRN) ──────────────────────────────────────────
    oa_papers = fetch_openalex_window(cfg, window_start, window_end)
    oa_new = 0
    for p in oa_papers:
        if p["arxiv_id"] not in seen:
            seen.add(p["arxiv_id"])
            results.append(p)
            oa_new += 1
    if oa_new:
        print(f"  [openalex] +{oa_new} non-arXiv papers", flush=True)

    return results


def fetch_recent(cfg: dict, days_override: int | None = None) -> list[dict]:
    """Fetch papers from the last N days (normal mode)."""
    days = days_override or cfg["days_lookback"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    client = arxiv.Client(page_size=200, delay_seconds=3, num_retries=5)
    results: list[dict] = []
    seen: set[str] = set()

    for profile_name, profile in _active_profiles(cfg):
        cats = profile.get("categories", [])
        keywords = profile.get("keywords", [])
        if not cats:
            continue
        query = " OR ".join(f"cat:{c}" for c in cats)
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
            if pid not in seen:
                if not keywords or matches_keywords(r, keywords):
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
    parser.add_argument("--fetch-ss", action="store_true",
                        help="One-time bulk import from Semantic Scholar (non-arXiv papers)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = init_db(cfg["db_path"])

    if args.fetch_ss:
        papers = fetch_semantic_scholar_bulk(cfg)
        new_count = 0
        for p in papers:
            if not already_fetched(conn, p["arxiv_id"]):
                save_paper(conn, p)
                new_count += 1
        print(f"\nSemantic Scholar import: {new_count} new papers added. DB total: {count_total(conn)}")
        conn.close()
        return 0

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
