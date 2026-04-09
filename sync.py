#!/usr/bin/env python3
"""
sync.py — Index all processed vault papers into ChromaDB for semantic search.

Run this after process.py to make new summaries searchable.
Safe to run multiple times — only indexes papers not yet in ChromaDB.

Usage:
    python sync.py              # sync all processed papers
    python sync.py --rebuild    # wipe and rebuild the entire index
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import chromadb
import yaml


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["vault_path"] = str(Path(cfg["vault_path"]).expanduser())
    return cfg


def get_collection(cfg: dict, rebuild: bool = False) -> chromadb.Collection:
    Path(cfg["chroma_path"]).mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=cfg["chroma_path"])
    if rebuild:
        try:
            client.delete_collection("quant_papers")
            print("Deleted existing index.")
        except Exception:
            pass
    return client.get_or_create_collection(
        name="quant_papers",
        metadata={"hnsw:space": "cosine"},
    )


def get_processed_papers(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT arxiv_id, title, authors, abstract, categories, published, vault_path
        FROM papers WHERE processed = 1 AND vault_path IS NOT NULL
    """).fetchall()
    conn.close()
    return [
        {
            "arxiv_id": r[0],
            "title": r[1],
            "authors": json.loads(r[2]),
            "abstract": r[3],
            "categories": json.loads(r[4]),
            "published": r[5][:10],
            "vault_path": r[6],
        }
        for r in rows
    ]


def already_indexed(collection: chromadb.Collection, arxiv_id: str) -> bool:
    try:
        result = collection.get(ids=[arxiv_id])
        return len(result["ids"]) > 0
    except Exception:
        return False


def index_paper(collection: chromadb.Collection, paper: dict) -> bool:
    vault_path = Path(paper["vault_path"])
    if not vault_path.exists():
        print(f"  Skipping {paper['arxiv_id']}: vault file not found at {vault_path}")
        return False

    content = vault_path.read_text(encoding="utf-8")

    # Use the full summary as the document for embedding
    collection.upsert(
        ids=[paper["arxiv_id"]],
        documents=[content],
        metadatas=[{
            "arxiv_id": paper["arxiv_id"],
            "title": paper["title"],
            "categories": ", ".join(paper["categories"]),
            "published": paper["published"],
            "vault_path": str(vault_path),
        }],
    )
    return True


def index_guidelines(collection: chromadb.Collection, vault_path: str, rebuild: bool) -> int:
    """Index all .md files in vault_path/guidelines/ directory."""
    guidelines_dir = Path(vault_path) / "guidelines"
    if not guidelines_dir.exists():
        return 0

    indexed = 0
    for md_file in guidelines_dir.glob("*.md"):
        doc_id = f"guideline::{md_file.stem}"
        if not rebuild and already_indexed(collection, doc_id):
            continue
        content = md_file.read_text(encoding="utf-8")
        collection.upsert(
            ids=[doc_id],
            documents=[content],
            metadatas=[{
                "arxiv_id": doc_id,
                "title": md_file.stem.replace("-", " ").title(),
                "categories": "guidelines",
                "published": "2026-01-01",
                "vault_path": str(md_file),
            }],
        )
        print(f"  + guideline: {md_file.name}")
        indexed += 1
    return indexed


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync vault papers into ChromaDB")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--rebuild", action="store_true", help="Wipe and rebuild index")
    args = parser.parse_args()

    cfg = load_config(args.config)
    collection = get_collection(cfg, rebuild=args.rebuild)
    papers = get_processed_papers(cfg["db_path"])

    if not papers:
        print("No processed papers found. Run `python process.py` first.")
        return

    existing_count = collection.count()
    print(f"Found {len(papers)} processed papers. Index currently has {existing_count} entries.")

    indexed = 0
    skipped = 0
    for paper in papers:
        if not args.rebuild and already_indexed(collection, paper["arxiv_id"]):
            skipped += 1
            continue
        if index_paper(collection, paper):
            indexed += 1
            print(f"  + {paper['arxiv_id']}: {paper['title'][:65]}")

    g_indexed = index_guidelines(collection, cfg["vault_path"], args.rebuild)
    indexed += g_indexed

    print(f"\nDone. Indexed {indexed} new papers/guidelines. Skipped {skipped} already indexed.")
    print(f"Total papers in index: {collection.count()}")


if __name__ == "__main__":
    main()
