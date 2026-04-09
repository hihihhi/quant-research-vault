#!/usr/bin/env python3
"""
research.py — Research CLI for the quant vault.

Commands:
    python research.py "momentum factor crypto"           semantic search
    python research.py --alpha-ideas "pairs trading"      search + Claude alpha generation
    python research.py --stats                            vault statistics
    python research.py --related 2401.12345               find related papers
    python research.py --daily                            today's new papers
    python research.py --top-crypto 10                    top N papers by Crypto score
    python research.py --export "momentum" out.md         export results to markdown
"""

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import chromadb
import yaml

# ── UTF-8 stdout fix ──────────────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"


# ── Config ────────────────────────────────────────────────────────────────────

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


# ── Display helpers ───────────────────────────────────────────────────────────

DIVIDER = "\u2501" * 60  # ━━━...


def print_paper(rank_or_score: str, title: str, arxiv_id: str,
                published: str, categories: str, excerpt: str) -> None:
    print(f"\n\u2501\u2501\u2501 [{rank_or_score}] {title[:70]} \u2501\u2501\u2501")
    date_str = published[:7] if published else "unknown"
    print(f"arXiv: {arxiv_id} | {date_str} | {categories}")
    if excerpt:
        # Clean excerpt: collapse whitespace, limit to 300 chars
        clean = " ".join(excerpt.split())
        print(clean[:300] + ("..." if len(clean) > 300 else ""))


# ── Search ────────────────────────────────────────────────────────────────────

def do_search(cfg: dict, query: str, n: int = 8) -> list[dict]:
    collection = get_collection(cfg)
    if collection.count() == 0:
        print("ChromaDB index is empty. Run `python run.py` to build it first.")
        return []

    results = collection.query(
        query_texts=[query],
        n_results=min(n, collection.count()),
        include=["documents", "metadatas", "distances"],
    )

    papers = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        papers.append({
            "arxiv_id": meta.get("arxiv_id", ""),
            "title": meta.get("title", ""),
            "categories": meta.get("categories", ""),
            "published": meta.get("published", ""),
            "vault_path": meta.get("vault_path", ""),
            "relevance_score": round(1 - dist, 3),
            "excerpt": doc[:400],
        })
    return papers


def cmd_search(cfg: dict, query: str) -> None:
    print(f'\nSearching vault: "{query}"')
    papers = do_search(cfg, query)
    if not papers:
        return
    print(f"\nFound {len(papers)} papers:\n")
    for p in papers:
        print_paper(
            f"{p['relevance_score']:.2f}",
            p["title"],
            p["arxiv_id"],
            p["published"],
            p["categories"],
            p["excerpt"],
        )
    print()


# ── Alpha ideas via Claude ────────────────────────────────────────────────────

def cmd_alpha_ideas(cfg: dict, topic: str) -> None:
    print(f'\nGenerating alpha ideas for: "{topic}"')
    print("Searching vault for relevant papers...")
    papers = do_search(cfg, topic, n=8)
    if not papers:
        print("No papers found in vault. Run `python run.py` first.")
        return

    paper_summaries = []
    for i, p in enumerate(papers, 1):
        # Prefer full vault file content; fall back to excerpt
        content = p["excerpt"]
        if p["vault_path"]:
            vp = Path(p["vault_path"])
            if vp.exists():
                content = vp.read_text(encoding="utf-8")[:2000]
        paper_summaries.append(
            f"--- Paper {i}: {p['title']} ({p['arxiv_id']}) ---\n{content}"
        )

    prompt = (
        f"You are a quant researcher specializing in systematic trading strategies.\n"
        f"Based on the following {len(papers)} research papers, generate 3 specific, "
        f"implementable alpha ideas for the topic: '{topic}'.\n\n"
        f"For each idea include:\n"
        f"1. Signal construction (exact steps)\n"
        f"2. Data required (source, frequency)\n"
        f"3. Expected holding period\n"
        f"4. Key risk and failure mode\n\n"
        f"{'=' * 40}\n\n"
        + "\n\n".join(paper_summaries)
    )

    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("ERROR: 'claude' CLI not found. Install it via: npm install -g @anthropic-ai/claude-code")
        return

    model = cfg.get("claude_model", "claude-haiku-4-5-20251001")
    print(f"\nAsking Claude ({model}) for alpha ideas...\n")
    print(DIVIDER)

    result = subprocess.run(
        [claude_bin, "--output-format", "text", "--model", model, "-p", "-"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=300,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        print(f"Claude CLI error: {result.stderr[:300]}")
        return

    print(result.stdout.strip())
    print(DIVIDER)


# ── Stats ─────────────────────────────────────────────────────────────────────

def cmd_stats(cfg: dict) -> None:
    conn = get_db(cfg)
    collection = get_collection(cfg)

    total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    processed = conn.execute(
        "SELECT COUNT(*) FROM papers WHERE processed = 1"
    ).fetchone()[0]
    abstract_only = conn.execute(
        "SELECT COUNT(*) FROM papers WHERE processed = 1 AND vault_path IS NOT NULL"
    ).fetchone()[0]

    date_range = conn.execute(
        "SELECT MIN(published), MAX(published) FROM papers"
    ).fetchone()
    min_date = (date_range[0] or "")[:10]
    max_date = (date_range[1] or "")[:10]

    index_size = collection.count()

    # Papers per year (last 5)
    year_rows = conn.execute("""
        SELECT substr(published, 1, 4) as yr, COUNT(*) as cnt
        FROM papers
        WHERE yr >= cast(strftime('%Y', 'now') - 5 as text)
        GROUP BY yr ORDER BY yr DESC
    """).fetchall()

    # Top 5 categories
    all_cats: dict[str, int] = {}
    for (cats_json,) in conn.execute("SELECT categories FROM papers").fetchall():
        import json
        try:
            cats = json.loads(cats_json)
        except Exception:
            continue
        for c in cats:
            all_cats[c] = all_cats.get(c, 0) + 1
    top_cats = sorted(all_cats.items(), key=lambda x: -x[1])[:5]

    conn.close()

    print("\n" + DIVIDER)
    print("  QUANT VAULT STATISTICS")
    print(DIVIDER)
    print(f"  Total papers in DB    : {total:,}")
    print(f"  Processed (any)       : {processed:,}")
    print(f"  With vault file       : {abstract_only:,}")
    print(f"  ChromaDB index size   : {index_size:,}")
    print(f"  Date range            : {min_date} to {max_date}")
    print()
    print("  Papers per year (last 5):")
    for yr, cnt in year_rows:
        bar = "#" * min(cnt // 50, 40)
        print(f"    {yr}  {cnt:>5,}  {bar}")
    print()
    print("  Top 5 categories:")
    for cat, cnt in top_cats:
        print(f"    {cat:<15}  {cnt:>5,}")
    print(DIVIDER)


# ── Related papers ────────────────────────────────────────────────────────────

def cmd_related(cfg: dict, arxiv_id: str) -> None:
    conn = get_db(cfg)
    row = conn.execute(
        "SELECT title, abstract FROM papers WHERE arxiv_id = ?", (arxiv_id,)
    ).fetchone()
    conn.close()

    if not row:
        print(f"Paper {arxiv_id} not found in DB.")
        return

    title, abstract = row
    print(f'\nFinding papers related to: {title}')
    query = f"{title} {abstract[:300]}"
    papers = do_search(cfg, query, n=8)

    # Filter out the paper itself
    papers = [p for p in papers if p["arxiv_id"] != arxiv_id][:6]
    if not papers:
        print("No related papers found.")
        return

    print(f"\nTop {len(papers)} related papers:\n")
    for p in papers:
        print_paper(
            f"{p['relevance_score']:.2f}",
            p["title"],
            p["arxiv_id"],
            p["published"],
            p["categories"],
            p["excerpt"],
        )
    print()


# ── Daily papers ──────────────────────────────────────────────────────────────

def cmd_daily(cfg: dict) -> None:
    conn = get_db(cfg)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT arxiv_id, title, categories, published, abstract
        FROM papers
        WHERE substr(fetched_at, 1, 10) >= ?
        ORDER BY published DESC
        LIMIT 50
    """, (yesterday,)).fetchall()
    conn.close()

    if not rows:
        print(f"No new papers fetched today ({today}).")
        return

    print(f"\n{len(rows)} papers fetched in the last 24 hours:\n")
    for r in rows:
        import json
        try:
            cats = ", ".join(json.loads(r[2]))
        except Exception:
            cats = r[2]
        print_paper("NEW", r[1], r[0], r[3], cats, r[4])
    print()


# ── Top crypto papers ─────────────────────────────────────────────────────────

def _extract_crypto_score(content: str) -> float:
    """Extract numeric crypto applicability score from vault file."""
    # Look for "Crypto Applicability\nRate X/5" pattern
    match = re.search(r"Crypto.*?Rate\s+(\d)[/ ]5", content, re.IGNORECASE | re.DOTALL)
    if match:
        return float(match.group(1))
    # Also look for "Crypto: X/5" in relevance score line
    match = re.search(r"Crypto:\s*(\d)[/ ]5", content, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return 0.0


def cmd_top_crypto(cfg: dict, n: int) -> None:
    conn = get_db(cfg)
    rows = conn.execute("""
        SELECT arxiv_id, title, categories, published, vault_path
        FROM papers
        WHERE processed = 1 AND vault_path IS NOT NULL
        ORDER BY published DESC
        LIMIT 500
    """).fetchall()
    conn.close()

    scored = []
    for r in rows:
        vp = Path(r[4]) if r[4] else None
        score = 0.0
        if vp and vp.exists():
            content = vp.read_text(encoding="utf-8")
            score = _extract_crypto_score(content)
        if score > 0:
            scored.append((score, r))

    scored.sort(key=lambda x: -x[0])
    top = scored[:n]

    if not top:
        print(f"No crypto-scored papers found (need Phase 2 processing).")
        return

    import json
    print(f"\nTop {len(top)} papers by Crypto Applicability score:\n")
    for score, r in top:
        try:
            cats = ", ".join(json.loads(r[2]))
        except Exception:
            cats = r[2]
        print_paper(f"Crypto {score:.0f}/5", r[1], r[0], r[3], cats, "")
    print()


# ── Export to markdown ────────────────────────────────────────────────────────

def cmd_export(cfg: dict, query: str, output_file: str) -> None:
    papers = do_search(cfg, query, n=20)
    if not papers:
        print("No papers found to export.")
        return

    lines = [
        f"# Quant Vault Export: {query}",
        f"",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"Results: {len(papers)} papers",
        f"",
        "---",
        "",
    ]
    for p in papers:
        lines.append(f"## [{p['relevance_score']:.2f}] {p['title']}")
        lines.append(f"")
        lines.append(f"- **arXiv:** [{p['arxiv_id']}](https://arxiv.org/abs/{p['arxiv_id']})")
        lines.append(f"- **Published:** {p['published'][:10]}")
        lines.append(f"- **Categories:** {p['categories']}")
        lines.append(f"")

        # Try to include full vault file
        if p["vault_path"]:
            vp = Path(p["vault_path"])
            if vp.exists():
                content = vp.read_text(encoding="utf-8")
                # Strip YAML frontmatter
                if content.startswith("---"):
                    end = content.find("---", 3)
                    content = content[end + 3:].strip() if end > 0 else content
                lines.append(content)
            else:
                lines.append(p["excerpt"])
        else:
            lines.append(p["excerpt"])
        lines.append("")
        lines.append("---")
        lines.append("")

    out_path = Path(output_file)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Exported {len(papers)} papers to {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Quant Research Vault — CLI research tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python research.py "momentum factor crypto"
  python research.py --alpha-ideas "pairs trading"
  python research.py --stats
  python research.py --related 2401.12345
  python research.py --daily
  python research.py --top-crypto 10
  python research.py --export "momentum" out.md
        """,
    )
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("--alpha-ideas", metavar="TOPIC",
                        help="Generate alpha ideas for a topic via Claude")
    parser.add_argument("--stats", action="store_true", help="Show vault statistics")
    parser.add_argument("--related", metavar="ARXIV_ID", help="Find related papers")
    parser.add_argument("--daily", action="store_true", help="Show today's new papers")
    parser.add_argument("--top-crypto", type=int, metavar="N",
                        help="Top N papers by crypto applicability score")
    parser.add_argument("--export", nargs=2, metavar=("QUERY", "OUTPUT"),
                        help="Export search results to a markdown file")
    args = parser.parse_args()

    cfg = load_config()

    if args.stats:
        cmd_stats(cfg)
    elif args.alpha_ideas:
        cmd_alpha_ideas(cfg, args.alpha_ideas)
    elif args.related:
        cmd_related(cfg, args.related)
    elif args.daily:
        cmd_daily(cfg)
    elif args.top_crypto:
        cmd_top_crypto(cfg, args.top_crypto)
    elif args.export:
        cmd_export(cfg, args.export[0], args.export[1])
    elif args.query:
        cmd_search(cfg, args.query)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
