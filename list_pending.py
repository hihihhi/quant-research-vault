#!/usr/bin/env python3
"""
list_pending.py — List papers pending full analysis (abstract-only vault entries).

Outputs JSON to stdout so Claude Code skill can read and process them.
Abstract-only entries have no ## Signal or ## Construction sections.

Usage:
    python list_pending.py                   # all pending papers (JSON)
    python list_pending.py --limit 50        # first 50 pending
    python list_pending.py --count-only      # just print the count
    python list_pending.py --domain q-fin    # filter by category prefix
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["vault_path"] = str(Path(cfg["vault_path"]).expanduser())
    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="List papers needing full analysis")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--limit", type=int, help="Max papers to list")
    parser.add_argument("--count-only", action="store_true", help="Print count only")
    parser.add_argument("--domain", help="Filter by category prefix (e.g. q-fin, cs, stat)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = sqlite3.connect(cfg["db_path"])

    q = (
        "SELECT arxiv_id, title, authors, abstract, categories, published, pdf_url, vault_path "
        "FROM papers WHERE processed=1 AND vault_path IS NOT NULL "
        "ORDER BY published DESC"
    )
    rows = conn.execute(q).fetchall()
    conn.close()

    pending = []
    for r in rows:
        vp = r[7]
        if not vp:
            continue
        cats = json.loads(r[4]) if r[4] else []
        if args.domain and not any(c.startswith(args.domain) for c in cats):
            continue
        try:
            content = Path(vp).read_text(encoding="utf-8", errors="replace")
            if "## Signal" not in content and "## Construction" not in content:
                pending.append({
                    "arxiv_id": r[0],
                    "title": r[1],
                    "categories": cats,
                    "published": r[5][:10] if r[5] else "",
                    "pdf_url": r[6],
                    "vault_path": vp,
                })
        except Exception:
            pass

        if args.limit and len(pending) >= args.limit:
            break

    if args.count_only:
        print(len(pending))
        return

    print(json.dumps(pending, indent=2, ensure_ascii=False))
    print(f"# {len(pending)} papers pending full analysis", file=sys.stderr)


if __name__ == "__main__":
    main()
