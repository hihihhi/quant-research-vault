#!/usr/bin/env python3
"""
mark_analyzed.py — Record that a paper has been fully analyzed.

Called by Claude Code after writing a full analysis to the vault markdown file.
This does NOT update processed=1 (that's already set by abstract-only phase).
Instead it just confirms the vault file now contains full analysis.

Usage:
    python mark_analyzed.py 2401.12345
    python mark_analyzed.py 2401.12345 /path/to/vault/file.md
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["vault_path"] = str(Path(cfg["vault_path"]).expanduser())
    return cfg


def verify_analyzed(vault_path: str) -> bool:
    """Confirm the vault file actually contains full analysis."""
    try:
        content = Path(vault_path).read_text(encoding="utf-8", errors="replace")
        return "## Signal" in content or "## Construction" in content
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Mark paper as fully analyzed")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("arxiv_id", help="arXiv ID (e.g. 2401.12345)")
    parser.add_argument("vault_path", nargs="?", help="Path to vault .md file (optional)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = sqlite3.connect(cfg["db_path"])

    if args.vault_path:
        vp = args.vault_path
    else:
        row = conn.execute(
            "SELECT vault_path FROM papers WHERE arxiv_id=?", (args.arxiv_id,)
        ).fetchone()
        if not row or not row[0]:
            print(f"ERROR: {args.arxiv_id} not found in DB or has no vault_path", file=sys.stderr)
            conn.close()
            sys.exit(1)
        vp = row[0]

    if not verify_analyzed(vp):
        print(
            f"WARNING: {vp} does not contain ## Signal or ## Construction.\n"
            "Are you sure this paper has been fully analyzed?",
            file=sys.stderr,
        )

    conn.execute(
        "UPDATE papers SET processed=1, vault_path=? WHERE arxiv_id=?",
        (vp, args.arxiv_id),
    )
    conn.commit()
    conn.close()
    print(f"OK: {args.arxiv_id} → {vp}")


if __name__ == "__main__":
    main()
