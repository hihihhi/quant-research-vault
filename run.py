#!/usr/bin/env python3
"""
run.py — Full pipeline: fetch → process → sync.

Usage:
    python run.py                    # full pipeline (14-day window)
    python run.py --fetch-only       # only fetch new paper metadata
    python run.py --process-only     # only process pending PDFs
    python run.py --sync-only        # only sync vault to ChromaDB
    python run.py --days 30          # fetch papers from last 30 days
    python run.py --limit 5          # process max 5 papers per run
    python run.py --all-history      # fetch ALL papers chunk by chunk, auto process+sync each batch
"""

import argparse
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

# Windows terminals may use cp950/cp1252 — force UTF-8 output
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent


def load_config(path: str = "config.yaml") -> dict:
    with open(ROOT / path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_step(script: str, extra_args: list[str]) -> int:
    cmd = [sys.executable, str(ROOT / script)] + extra_args
    print(f"\n{'='*60}", flush=True)
    print(f">> {' '.join(cmd)}", flush=True)
    print('='*60, flush=True)
    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode


def count_db(db_path: str) -> tuple[int, int]:
    """Return (total, unprocessed) from DB."""
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM papers WHERE processed = 0").fetchone()[0]
    conn.close()
    return total, pending


def date_windows(start: date, end: date, chunk_months: int = 3) -> list[tuple[date, date]]:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Full quant research pipeline")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--process-only", action="store_true")
    parser.add_argument("--sync-only", action="store_true")
    parser.add_argument("--days", type=int, help="Override days_lookback for fetch")
    parser.add_argument("--limit", type=int, help="Max papers to process per run")
    parser.add_argument("--dry-run", action="store_true", help="Fetch dry run only")
    parser.add_argument("--all-history", action="store_true",
                        help="Fetch entire arXiv history chunk by chunk, process+sync each chunk")
    parser.add_argument("--abstract-only", action="store_true",
                        help="Use abstract-only mode for processing (fast, no PDF/Claude). Recommended for --all-history")
    args = parser.parse_args()

    cfg = load_config(args.config)
    config_args = ["--config", args.config]
    run_all = not (args.fetch_only or args.process_only or args.sync_only)

    # ── All-history mode: chunk-by-chunk fetch → process → sync ───────────────
    if args.all_history:
        # q-fin categories started in 1997; skip empty years before that
        start_date = date(1997, 1, 1)
        end_date = date.today()
        windows = date_windows(start_date, end_date, chunk_months=3)
        print(f"All-history pipeline: {len(windows)} quarterly windows ({start_date} to {end_date})")
        print("Each window: fetch -> process -> sync\n")

        import time as _time
        for i, (w_start, w_end) in enumerate(windows, 1):
            print(f"\n{'#'*60}", flush=True)
            print(f"# Chunk {i}/{len(windows)}: {w_start} -> {w_end}", flush=True)
            print(f"{'#'*60}", flush=True)

            # Polite delay between chunks (arXiv rate limit: be gentle)
            if i > 1:
                _time.sleep(20)

            # 1. Fetch this window
            fetch_args = config_args + [
                "--window-start", w_start.strftime("%Y%m%d"),
                "--window-end",   w_end.strftime("%Y%m%d"),
            ]
            rc = run_step("fetch.py", fetch_args)
            if rc != 0:
                print(f"Fetch failed for chunk {i}. Waiting 120s then continuing...", flush=True)
                _time.sleep(120)
                continue

            total, pending = count_db(cfg["db_path"])
            print(f"DB: {total} total, {pending} unprocessed", flush=True)

            if pending == 0:
                print("No new papers to process, skipping.", flush=True)
                continue

            # 2. Process pending papers
            process_args = config_args + ["--limit", str(args.limit or 500)]
            if args.abstract_only:
                process_args.append("--abstract-only")
            rc = run_step("process.py", process_args)
            if rc != 0:
                print(f"Process step failed for chunk {i}.", flush=True)

            # 3. Sync to ChromaDB
            rc = run_step("sync.py", config_args)
            if rc != 0:
                print(f"Sync step failed for chunk {i}.", flush=True)

            total, pending = count_db(cfg["db_path"])
            print(f"\n[Chunk {i}/{len(windows)} done] DB: {total} total, {pending} still pending", flush=True)

        total, pending = count_db(cfg["db_path"])
        print(f"\nAll-history pipeline complete. DB: {total} total, {pending} pending.")
        return

    # ── Normal mode ───────────────────────────────────────────────────────────
    if run_all or args.fetch_only:
        fetch_args = config_args[:]
        if args.days:
            fetch_args += ["--days", str(args.days)]
        if args.dry_run:
            fetch_args.append("--dry-run")
        rc = run_step("fetch.py", fetch_args)
        if rc != 0:
            print(f"\nFetch failed (exit {rc}). Stopping.")
            sys.exit(rc)

    if args.fetch_only or args.dry_run:
        return

    if run_all or args.process_only:
        process_args = config_args[:]
        if args.limit:
            process_args += ["--limit", str(args.limit)]
        rc = run_step("process.py", process_args)
        if rc != 0:
            print(f"\nProcess failed (exit {rc}). Stopping.")
            sys.exit(rc)

    if run_all or args.sync_only:
        rc = run_step("sync.py", config_args)
        if rc != 0:
            print(f"\nSync failed (exit {rc}). Stopping.")
            sys.exit(rc)

    print("\nPipeline complete.")


if __name__ == "__main__":
    main()
