#!/usr/bin/env python3
"""
process.py — Build the quantitative knowledge vault.

Two-phase design:
  Phase 1 (--abstract-only): Index all papers instantly using abstracts.
                              Gets 40k papers searchable in ~1-2 hours.
  Phase 2 (--workers N):     Parallel Claude enrichment — downloads full PDF,
                              generates structured trading analysis alongside
                              the abstract. Upgrades abstract-only entries.

Usage:
    python process.py --abstract-only          # Phase 1: fast index all pending
    python process.py --workers 3              # Phase 2: full analysis, 3 parallel
    python process.py --workers 5 --limit 100  # Full analysis, 5 workers, 100 papers
    python process.py --arxiv-id 2401.12345    # Single paper, full analysis
    python process.py --upgrade                # Re-enrich abstract-only entries
"""

import argparse
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Active subprocess registry (for Ctrl+C cleanup) ──────────────────────────
_active_procs: list["subprocess.Popen"] = []
_procs_lock = threading.Lock()


def _register_proc(p: "subprocess.Popen") -> None:
    with _procs_lock:
        _active_procs.append(p)


def _deregister_proc(p: "subprocess.Popen") -> None:
    with _procs_lock:
        try:
            _active_procs.remove(p)
        except ValueError:
            pass


def _kill_all_active() -> None:
    with _procs_lock:
        for p in list(_active_procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()


def _install_sigint() -> None:
    def _handler(sig, frame):  # noqa: ANN001
        print("\n[process.py] Ctrl+C — killing active Claude processes...", flush=True)
        _kill_all_active()
        sys.exit(1)
    signal.signal(signal.SIGINT, _handler)

import pdfplumber
import requests
import yaml


# ── Prompts ───────────────────────────────────────────────────────────────────

SUMMARY_PROMPT = textwrap.dedent("""
You are the quantitative brain of an autonomous trading firm specializing in
crypto and HK equities. Your job is to extract every exploitable insight from
this research paper and turn it into actionable intelligence for our signals team.

Paper text:
---
{text}
---

Output ONLY the following markdown (no preamble, no trailing commentary):

## Signal / Alpha Idea
One paragraph: what trading signal, factor, or strategy does this paper propose?
Be specific about directionality, holding period, asset class.

## Construction
Exact recipe: inputs required, calculation steps, key formulas. Be concrete enough
that a quant dev could implement this from your description alone.

## Key Parameters
| Parameter | Value | Notes |
|-----------|-------|-------|
(lookback windows, thresholds, rebalance frequency, hyperparameters)

## Empirical Results
- Best reported Sharpe / return / hit rate (state the exact number + sample period)
- Statistical significance (t-stat, p-value, bootstrap CI if reported)
- Transaction cost assumption used in the paper

## Failure Modes & Risks
- When does this alpha decay or reverse? (market regime, crowding, etc.)
- Data requirements that may be unavailable or expensive
- Overfitting / publication bias concerns

## Regime Conditions
Bull/bear, high/low vol, trending/mean-reverting — when is this strongest / weakest?

## Crypto Applicability
Rate 1-5 and explain: can this be adapted for BTC/ETH/altcoin markets?
What adaptations are needed for 24/7 markets, thin books, higher vol?

## HK Equity Applicability
Rate 1-5 and explain: relevant for HKEX / H-shares / Hang Seng constituents?

## Implementation Checklist
- [ ] Data source required
- [ ] Compute complexity (O(n), realtime vs batch)
- [ ] Infrastructure dependencies
- [ ] Estimated time to implement

## Relevance Score
Novelty: X/5 | Feasibility: X/5 | Crypto: X/5 | HK Equity: X/5
""").strip()


# ── Config & DB ───────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["vault_path"] = str(Path(cfg["vault_path"]).expanduser())
    return cfg


def get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return conn


def get_pending(conn: sqlite3.Connection, limit: int | None = None,
                arxiv_id: str | None = None, upgrade: bool = False) -> list[dict]:
    if arxiv_id:
        rows = conn.execute(
            "SELECT arxiv_id,title,authors,abstract,categories,published,pdf_url "
            "FROM papers WHERE arxiv_id=?", (arxiv_id,)
        ).fetchall()
    elif upgrade:
        # Re-process entries written in abstract-only mode
        q = ("SELECT arxiv_id,title,authors,abstract,categories,published,pdf_url "
             "FROM papers WHERE processed=1 AND vault_path IS NOT NULL "
             "ORDER BY published DESC")
        if limit:
            q += f" LIMIT {limit}"
        rows = conn.execute(q).fetchall()
    else:
        q = ("SELECT arxiv_id,title,authors,abstract,categories,published,pdf_url "
             "FROM papers WHERE processed=0 ORDER BY published DESC")
        if limit:
            q += f" LIMIT {limit}"
        rows = conn.execute(q).fetchall()
    return [
        {"arxiv_id": r[0], "title": r[1], "authors": json.loads(r[2]),
         "abstract": r[3], "categories": json.loads(r[4]),
         "published": r[5], "pdf_url": r[6]}
        for r in rows
    ]


_db_lock = threading.Lock()

def mark_processed(conn: sqlite3.Connection, arxiv_id: str, vault_path: str) -> None:
    with _db_lock:
        conn.execute(
            "UPDATE papers SET processed=1, vault_path=? WHERE arxiv_id=?",
            (vault_path, arxiv_id)
        )
        conn.commit()


# ── PDF extraction ────────────────────────────────────────────────────────────

def download_pdf(pdf_url: str) -> bytes | None:
    try:
        r = requests.get(pdf_url, timeout=30,
                         headers={"User-Agent": "quant-research-vault/1.0"})
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"    PDF download failed: {e}", flush=True)
        return None


def extract_text(pdf_bytes: bytes, max_chars: int) -> str:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = pdf.pages
            target = pages[:6] + pages[-2:] if len(pages) > 8 else pages
            text = "\n\n".join(p.extract_text() or "" for p in target)
        return text[:max_chars]
    except Exception as e:
        print(f"    PDF extraction failed: {e}", flush=True)
        return ""


# ── Summarization ─────────────────────────────────────────────────────────────

def summarize(paper: dict, pdf_text: str, cfg: dict) -> str:
    source = pdf_text.strip() if pdf_text.strip() else (
        f"Title: {paper['title']}\n\nAbstract: {paper['abstract']}"
    )
    prompt = SUMMARY_PROMPT.format(text=source)
    model = cfg["claude_model"]

    if shutil.which("claude"):
        claude_bin = shutil.which("claude")
        proc = subprocess.Popen(
            [claude_bin, "--output-format", "text", "--model", model, "-p", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _register_proc(proc)
        try:
            stdout, stderr = proc.communicate(
                input=prompt.encode("utf-8", errors="replace"), timeout=240
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RuntimeError("claude CLI timed out after 240s")
        finally:
            _deregister_proc(proc)
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI error: {stderr[:300].decode('utf-8', errors='replace')}")
        return stdout.decode("utf-8", errors="replace").strip()

    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model, max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    raise RuntimeError(
        "No Claude auth. Install claude CLI (Max plan) or set ANTHROPIC_API_KEY."
    )


# ── Markdown builders ─────────────────────────────────────────────────────────

def _header(paper: dict) -> str:
    authors = paper["authors"]
    categories = paper["categories"]
    published = paper["published"][:10]
    return (
        f"---\n"
        f"arxiv_id: {paper['arxiv_id']}\n"
        f"title: \"{paper['title'].replace(chr(34), chr(39))}\"\n"
        f"authors: {', '.join(authors)}\n"
        f"published: {published}\n"
        f"categories: {', '.join(categories)}\n"
        f"source: https://arxiv.org/abs/{paper['arxiv_id']}\n"
        f"pdf: {paper['pdf_url']}\n"
        f"---\n\n"
        f"# {paper['title']}\n\n"
        f"**Authors:** {', '.join(authors)}  \n"
        f"**Published:** {published} | "
        f"**arXiv:** [{paper['arxiv_id']}](https://arxiv.org/abs/{paper['arxiv_id']})  \n"
        f"**Categories:** {', '.join(categories)}\n\n"
    )


def build_abstract_entry(paper: dict) -> str:
    return (
        _header(paper)
        + f"## Abstract\n\n{paper['abstract'].strip()}\n"
    )


def build_full_entry(paper: dict, summary: str) -> str:
    return (
        _header(paper)
        + f"## Abstract\n\n{paper['abstract'].strip()}\n\n"
        + "---\n\n"
        + summary
    )


# ── File path ─────────────────────────────────────────────────────────────────

def vault_file_path(paper: dict, vault_path: str, research_dir: str) -> Path:
    cat = next((c for c in paper["categories"] if c.startswith("q-fin")),
               paper["categories"][0])
    date_prefix = paper["published"][:7]
    slug = paper["arxiv_id"].replace("/", "-").replace(".", "-")
    folder = Path(vault_path) / research_dir / cat
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{date_prefix}-{slug}.md"


# ── Single-paper worker ───────────────────────────────────────────────────────

_print_lock = threading.Lock()

def _is_fully_analyzed(vault_path: str) -> bool:
    """Check if a vault file already has full Claude analysis (not just abstract)."""
    try:
        content = Path(vault_path).read_text(encoding="utf-8", errors="replace")
        return "## Signal" in content or "## Construction" in content
    except Exception:
        return False


def _process_one(paper: dict, cfg: dict, abstract_only: bool,
                 conn: sqlite3.Connection, counter: list) -> bool:
    try:
        out_path = vault_file_path(paper, cfg["vault_path"], cfg["research_dir"])

        # Skip Phase 2 if already fully analyzed (safe to restart without re-processing)
        if not abstract_only and out_path.exists() and _is_fully_analyzed(str(out_path)):
            with _print_lock:
                counter[0] += 1
            return True

        if abstract_only:
            markdown = build_abstract_entry(paper)
        else:
            pdf_text = ""
            if cfg.get("fetch_pdf", True):
                pdf_bytes = download_pdf(paper["pdf_url"])
                if pdf_bytes:
                    pdf_text = extract_text(pdf_bytes, cfg["max_pdf_chars"])
            summary = summarize(paper, pdf_text, cfg)
            markdown = build_full_entry(paper, summary)

        out_path.write_text(markdown, encoding="utf-8")
        mark_processed(conn, paper["arxiv_id"], str(out_path))

        with _print_lock:
            counter[0] += 1
            n = counter[0]
            total = counter[1]
            print(f"  [{n}/{total}] {paper['arxiv_id']}: {paper['title'][:60]}", flush=True)
        return True

    except Exception as e:
        with _print_lock:
            print(f"  ERROR {paper['arxiv_id']}: {e}", flush=True)
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def _safe_worker_count(requested: int) -> int:
    """Calculate a machine-safe Claude worker count.

    Each worker spawns one Claude CLI (node.js) process.
    Too many workers = process storm, RAM exhaustion, unusable PC.
    Cap based on available RAM and CPU so this is safe on any machine.
    """
    import psutil  # optional — only used here
    cpu = os.cpu_count() or 2
    try:
        ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        # Each claude process needs ~300-500 MB
        ram_cap = max(1, int(ram_gb / 0.5))
    except Exception:
        ram_cap = 3
    cpu_cap = max(1, cpu // 2)
    safe = min(requested, cpu_cap, ram_cap, 5)  # never exceed 5 regardless
    if safe < requested:
        print(
            f"[workers] Capped from {requested} → {safe} "
            f"(CPU cores/2={cpu_cap}, RAM cap={ram_cap}, hard max=5)",
            flush=True,
        )
    return safe


def main() -> None:
    _install_sigint()
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--arxiv-id")
    parser.add_argument("--abstract-only", action="store_true",
                        help="Phase 1: fast index (abstract only, no Claude)")
    parser.add_argument("--upgrade", action="store_true",
                        help="Re-enrich already-processed papers with full Claude analysis")
    parser.add_argument("--workers", type=int, default=None,
                        help="Parallel Claude workers (default: auto-detect from RAM/CPU)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = get_db(cfg["db_path"])
    papers = get_pending(conn, limit=args.limit, arxiv_id=args.arxiv_id,
                         upgrade=args.upgrade)

    if not papers:
        print("No papers pending processing.")
        return

    abstract_only = args.abstract_only

    if abstract_only:
        workers = 1
    else:
        requested = args.workers if args.workers is not None else 2
        try:
            workers = _safe_worker_count(requested)
        except ImportError:
            # psutil not installed — fall back to conservative default
            workers = min(requested, 2)
            print(f"[workers] psutil not found; defaulting to {workers} workers. "
                  f"Install psutil for auto-detection.", flush=True)

    mode = "abstract-only" if abstract_only else f"full PDF+Claude ({workers} workers)"
    print(f"Processing {len(papers)} papers [{mode}]...", flush=True)

    counter = [0, len(papers)]  # [done, total]

    if workers == 1:
        for paper in papers:
            _process_one(paper, cfg, abstract_only, conn, counter)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_process_one, p, cfg, abstract_only, conn, counter): p
                for p in papers
            }
            for f in as_completed(futures):
                f.result()  # surface exceptions

    conn.close()
    print(f"\nDone. Processed {counter[0]}/{len(papers)} papers.")


if __name__ == "__main__":
    main()
