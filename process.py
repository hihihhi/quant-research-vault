#!/usr/bin/env python3
"""
process.py — Download PDFs and generate structured markdown summaries via Claude.

For each unprocessed paper in the DB:
  1. Download PDF from arXiv
  2. Extract text with pdfplumber
  3. Send to Claude Haiku with a structured prompt
  4. Save the markdown summary

Usage:
    python process.py                        # process all pending
    python process.py --limit 10             # process up to 10 papers
    python process.py --arxiv-id 2601.12345  # process a specific paper
"""

import argparse
import io
import sqlite3
import textwrap
import time
from pathlib import Path

import anthropic
import pdfplumber
import requests
import yaml


SUMMARY_PROMPT = textwrap.dedent("""
You are a quant finance research assistant for a systematic trading firm that builds
alpha signals for crypto and equity markets. Summarize this research paper into
actionable intelligence for our trading team.

Paper text:
---
{text}
---

Output ONLY the following markdown (no preamble, no trailing commentary):

## Signal / Alpha Idea
One paragraph: what trading signal, factor, or strategy does this paper propose?

## Construction
How is the signal constructed? Include key formulas, inputs, and logic. Be concrete.

## Key Parameters
| Parameter | Recommended Value | Notes |
|-----------|------------------|-------|
(fill in hyperparameters, lookback windows, thresholds, etc.)

## Key Findings
- (bullet: main empirical result with numbers)
- (bullet: Sharpe / return / hit rate if reported)
- (bullet: statistical significance / robustness)

## Failure Modes
- (bullet: when does this signal break down?)
- (bullet: known limitations or caveats)

## Regime Conditions
When is this signal strongest / weakest? (bull/bear, high/low vol, trending/mean-reverting)

## Crypto / HK Equity Applicability
Is this applicable to crypto or HK equities? What adaptations are needed?

## Implementation Notes
What data, compute, or infrastructure is needed to implement this?

## Relevance Score
Rate 1-5 for: (a) Signal novelty, (b) Implementation feasibility, (c) Crypto applicability
Format: Novelty: X/5 | Feasibility: X/5 | Crypto: X/5
""").strip()


# ── Config & DB ───────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_db(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def get_pending(conn: sqlite3.Connection, limit: int | None = None, arxiv_id: str | None = None) -> list[dict]:
    if arxiv_id:
        rows = conn.execute(
            "SELECT arxiv_id, title, authors, abstract, categories, published, pdf_url FROM papers WHERE arxiv_id = ?",
            (arxiv_id,)
        ).fetchall()
    else:
        q = "SELECT arxiv_id, title, authors, abstract, categories, published, pdf_url FROM papers WHERE processed = 0 ORDER BY published DESC"
        if limit:
            q += f" LIMIT {limit}"
        rows = conn.execute(q).fetchall()
    import json
    return [{"arxiv_id": r[0], "title": r[1], "authors": json.loads(r[2]),
             "abstract": r[3], "categories": json.loads(r[4]),
             "published": r[5], "pdf_url": r[6]} for r in rows]


def mark_processed(conn: sqlite3.Connection, arxiv_id: str, vault_path: str) -> None:
    conn.execute(
        "UPDATE papers SET processed = 1, vault_path = ? WHERE arxiv_id = ?",
        (vault_path, arxiv_id)
    )
    conn.commit()


# ── PDF extraction ────────────────────────────────────────────────────────────

def download_pdf(pdf_url: str) -> bytes | None:
    try:
        r = requests.get(pdf_url, timeout=30, headers={"User-Agent": "quant-research-vault/1.0"})
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"    PDF download failed: {e}")
        return None


def extract_text(pdf_bytes: bytes, max_chars: int) -> str:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = pdf.pages
            # Extract intro + methods + conclusion (first 6 + last 2 pages)
            target_pages = pages[:6] + pages[-2:] if len(pages) > 8 else pages
            text = "\n\n".join(
                p.extract_text() or "" for p in target_pages
            )
        return text[:max_chars]
    except Exception as e:
        print(f"    PDF extraction failed: {e}")
        return ""


# ── Summarization ─────────────────────────────────────────────────────────────

def summarize(paper: dict, text: str, cfg: dict) -> str:
    client = anthropic.Anthropic()

    # If PDF extraction failed, fall back to abstract only
    source_text = text if text.strip() else f"Title: {paper['title']}\n\nAbstract: {paper['abstract']}"

    msg = client.messages.create(
        model=cfg["claude_model"],
        max_tokens=2000,
        messages=[{"role": "user", "content": SUMMARY_PROMPT.format(text=source_text)}],
    )
    return msg.content[0].text


# ── Markdown output ───────────────────────────────────────────────────────────

def build_markdown(paper: dict, summary: str) -> str:
    import json
    authors = paper["authors"] if isinstance(paper["authors"], list) else json.loads(paper["authors"])
    published = paper["published"][:10]
    categories = paper["categories"] if isinstance(paper["categories"], list) else json.loads(paper["categories"])

    header = f"""---
arxiv_id: {paper['arxiv_id']}
title: "{paper['title'].replace('"', "'")}"
authors: {', '.join(authors)}
published: {published}
categories: {', '.join(categories)}
source: https://arxiv.org/abs/{paper['arxiv_id']}
pdf: {paper['pdf_url']}
---

# {paper['title']}

**Authors:** {', '.join(authors)}
**Published:** {published} | **arXiv:** [{paper['arxiv_id']}](https://arxiv.org/abs/{paper['arxiv_id']})
**Categories:** {', '.join(categories)}

"""
    return header + summary


def vault_file_path(paper: dict, vault_path: str, research_dir: str) -> Path:
    categories = paper["categories"] if isinstance(paper["categories"], list) else __import__('json').loads(paper["categories"])
    # Use first q-fin category, or first category
    cat = next((c for c in categories if c.startswith("q-fin")), categories[0])
    date_prefix = paper["published"][:7]  # YYYY-MM
    slug = paper["arxiv_id"].replace("/", "-").replace(".", "-")
    folder = Path(vault_path) / research_dir / cat
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{date_prefix}-{slug}.md"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Process papers into vault summaries")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--arxiv-id", help="Process a specific paper")
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = get_db(cfg["db_path"])
    papers = get_pending(conn, limit=args.limit, arxiv_id=args.arxiv_id)

    if not papers:
        print("No papers pending processing.")
        return

    print(f"Processing {len(papers)} papers...")

    for i, paper in enumerate(papers, 1):
        print(f"\n[{i}/{len(papers)}] {paper['arxiv_id']}: {paper['title'][:65]}")

        # Download PDF
        pdf_text = ""
        if cfg.get("fetch_pdf", True):
            print("  Downloading PDF...")
            pdf_bytes = download_pdf(paper["pdf_url"])
            if pdf_bytes:
                pdf_text = extract_text(pdf_bytes, cfg["max_pdf_chars"])
                print(f"  Extracted {len(pdf_text)} chars")

        # Summarize
        print("  Summarizing with Claude...")
        try:
            summary = summarize(paper, pdf_text, cfg)
        except Exception as e:
            print(f"  Summarization failed: {e}")
            continue

        # Write to vault
        out_path = vault_file_path(paper, cfg["vault_path"], cfg["research_dir"])
        markdown = build_markdown(paper, summary)
        out_path.write_text(markdown, encoding="utf-8")
        print(f"  Written to {out_path}")

        # Mark processed
        mark_processed(conn, paper["arxiv_id"], str(out_path))

        # Rate limit: 1 req/s to be polite to Claude API
        if i < len(papers):
            time.sleep(1)

    conn.close()
    print(f"\nDone. Processed {len(papers)} papers.")


if __name__ == "__main__":
    main()
