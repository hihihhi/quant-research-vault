#!/usr/bin/env python3
"""
generate-copilot-context.py — Generate a Copilot-compatible brain summary from the vault.

Writes to:
  - <vault_repo>/.github/memory/quant-brain.md
  - C:/Users/heiwa/Desktop/copilot-setup/.github/memory/quant-brain.md (if it exists)

Usage:
    python scripts/generate-copilot-context.py
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import chromadb
import yaml

# ── UTF-8 stdout fix ──────────────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"


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


def truncate(text: str, max_chars: int) -> str:
    text = " ".join(text.split())  # collapse whitespace
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "..."


def get_all_stats(conn: sqlite3.Connection, collection: chromadb.Collection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    processed = conn.execute(
        "SELECT COUNT(*) FROM papers WHERE processed = 1"
    ).fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(published), MAX(published) FROM papers"
    ).fetchone()

    all_cats: dict[str, int] = {}
    for (cats_json,) in conn.execute("SELECT categories FROM papers").fetchall():
        try:
            cats = json.loads(cats_json)
        except Exception:
            continue
        for c in cats:
            all_cats[c] = all_cats.get(c, 0) + 1

    return {
        "total": total,
        "processed": processed,
        "min_date": (date_range[0] or "")[:10],
        "max_date": (date_range[1] or "")[:10],
        "index_size": collection.count(),
        "categories": all_cats,
    }


def get_papers_by_category(
    conn: sqlite3.Connection, cfg: dict
) -> dict[str, list[dict]]:
    """Return top 5 papers per q-fin category."""
    categories = cfg.get("categories", [])
    result: dict[str, list[dict]] = {}

    for cat in categories:
        rows = conn.execute("""
            SELECT p.arxiv_id, p.title, p.abstract, p.published, p.vault_path
            FROM papers p
            WHERE p.processed = 1
              AND p.categories LIKE ?
            ORDER BY p.published DESC
            LIMIT 5
        """, (f'%"{cat}"%',)).fetchall()

        papers = []
        for r in rows:
            abstract = r[2] or ""
            # Try vault file for a richer abstract
            if r[4]:
                vp = Path(r[4])
                if vp.exists():
                    content = vp.read_text(encoding="utf-8")
                    # Extract abstract section
                    if "## Abstract" in content:
                        idx = content.index("## Abstract") + len("## Abstract")
                        abstract_section = content[idx:].split("##")[0].strip()
                        abstract = abstract_section[:400]
            papers.append({
                "arxiv_id": r[0],
                "title": r[1],
                "abstract": truncate(abstract, 200),
                "published": (r[3] or "")[:7],
            })
        if papers:
            result[cat] = papers

    return result


def get_recent_papers(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT arxiv_id, title, categories, published, abstract
        FROM papers
        WHERE fetched_at > ?
        ORDER BY published DESC
        LIMIT 20
    """, (cutoff,)).fetchall()

    result = []
    for r in rows:
        try:
            cats = ", ".join(json.loads(r[2]))
        except Exception:
            cats = r[2] or ""
        result.append({
            "arxiv_id": r[0],
            "title": r[1],
            "categories": cats,
            "published": (r[3] or "")[:7],
            "abstract": truncate(r[4] or "", 150),
        })
    return result


def get_top50_from_chroma(collection: chromadb.Collection) -> list[dict]:
    """Get first 50 indexed papers from ChromaDB."""
    count = collection.count()
    if count == 0:
        return []
    n = min(50, count)
    result = collection.get(
        limit=n,
        include=["documents", "metadatas"],
    )
    papers = []
    for doc, meta in zip(result["documents"], result["metadatas"]):
        papers.append({
            "arxiv_id": meta.get("arxiv_id", ""),
            "title": meta.get("title", ""),
            "categories": meta.get("categories", ""),
            "published": meta.get("published", ""),
            "excerpt": doc[:300] if doc else "",
        })
    return papers


CAT_NAMES = {
    "q-fin.TR": "Trading & Microstructure",
    "q-fin.PM": "Portfolio Management",
    "q-fin.ST": "Statistical Finance",
    "q-fin.RM": "Risk Management",
    "q-fin.CP": "Computational Finance",
    "q-fin.MF": "Mathematical Finance",
}


def build_markdown(
    stats: dict,
    papers_by_cat: dict[str, list[dict]],
    recent: list[dict],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Quant Research Brain — Auto-generated {now}",
        "",
        "> This file is auto-generated by `scripts/generate-copilot-context.py`.",
        "> It provides Copilot with a snapshot of the quant research vault.",
        "",
        "## Vault Statistics",
        "",
        f"- **Total papers indexed:** {stats['total']:,}",
        f"- **With full Claude analysis:** {stats['processed']:,}",
        f"- **ChromaDB index size:** {stats['index_size']:,}",
        f"- **Date range:** {stats['min_date']} to {stats['max_date']}",
        "",
    ]

    # Category breakdown
    lines += ["### Papers by Category", ""]
    all_cats = stats.get("categories", {})
    for cat, count in sorted(all_cats.items(), key=lambda x: -x[1])[:10]:
        name = CAT_NAMES.get(cat, cat)
        lines.append(f"- **{cat}** ({name}): {count:,} papers")
    lines.append("")

    # Papers by category
    lines += ["---", "", "## Research by Category", ""]
    for cat, papers in papers_by_cat.items():
        cat_name = CAT_NAMES.get(cat, cat)
        lines.append(f"### {cat} — {cat_name}")
        lines.append("")
        for p in papers:
            lines.append(
                f"**[{p['published']}] [{p['title']}]"
                f"(https://arxiv.org/abs/{p['arxiv_id']})**  "
            )
            lines.append(p["abstract"])
            lines.append("")
        lines.append("")

    # Recent papers
    if recent:
        lines += ["---", "", "## Recent Papers (last 30 days)", ""]
        for p in recent:
            lines.append(
                f"- **{p['published']}** [{p['title']}]"
                f"(https://arxiv.org/abs/{p['arxiv_id']}) "
                f"— {p['categories']}"
            )
            if p["abstract"]:
                lines.append(f"  {p['abstract']}")
        lines.append("")

    lines += [
        "---",
        "",
        "## How to Use This Brain",
        "",
        "- **Claude Code MCP:** `search_papers('momentum crypto')` — semantic search",
        "- **CLI:** `python research.py 'volatility forecasting'`",
        "- **Alpha ideas:** `python research.py --alpha-ideas 'pairs trading'`",
        "- **Stats:** `python research.py --stats`",
        "",
    ]

    return "\n".join(lines)


def write_output(content: str) -> None:
    # Primary: vault repo .github/memory/
    primary = ROOT / ".github" / "memory" / "quant-brain.md"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text(content, encoding="utf-8")
    print(f"Written: {primary}")

    # Secondary: copilot-setup repo (if it exists)
    secondary = Path("C:/Users/heiwa/Desktop/copilot-setup/.github/memory/quant-brain.md")
    if secondary.parent.parent.parent.exists():
        secondary.parent.mkdir(parents=True, exist_ok=True)
        secondary.write_text(content, encoding="utf-8")
        print(f"Written: {secondary}")
    else:
        print(f"Skipped: {secondary} (parent dir does not exist)")


def main() -> None:
    print("Generating Copilot context from quant vault...")
    cfg = load_config()
    collection = get_collection(cfg)
    conn = get_db(cfg)

    stats = get_all_stats(conn, collection)
    print(f"  Total papers: {stats['total']:,} | Processed: {stats['processed']:,} | "
          f"Index: {stats['index_size']:,}")

    papers_by_cat = get_papers_by_category(conn, cfg)
    recent = get_recent_papers(conn, days=30)
    conn.close()

    md = build_markdown(stats, papers_by_cat, recent)
    write_output(md)

    lines_count = md.count("\n")
    print(f"\nDone. Generated {lines_count} lines of context.")


if __name__ == "__main__":
    main()
