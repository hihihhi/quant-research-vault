#!/usr/bin/env python3
"""
run.py — Full pipeline: fetch → process → sync.

Run this on a schedule (daily cron) to keep the vault up to date.

Usage:
    python run.py                    # full pipeline
    python run.py --fetch-only       # only fetch new paper metadata
    python run.py --process-only     # only process pending PDFs
    python run.py --sync-only        # only sync vault to ChromaDB
    python run.py --days 30          # fetch papers from last 30 days
    python run.py --limit 5          # process max 5 papers per run
"""

import argparse
import subprocess
import sys
from pathlib import Path


def run_step(script: str, extra_args: list[str]) -> int:
    cmd = [sys.executable, script] + extra_args
    print(f"\n{'='*60}")
    print(f"Running: {' '.join(cmd)}")
    print('='*60)
    result = subprocess.run(cmd, cwd=Path(__file__).parent)
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Full quant research pipeline")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--process-only", action="store_true")
    parser.add_argument("--sync-only", action="store_true")
    parser.add_argument("--days", type=int, help="Override days_lookback for fetch")
    parser.add_argument("--limit", type=int, help="Max papers to process per run")
    parser.add_argument("--dry-run", action="store_true", help="Fetch dry run only")
    args = parser.parse_args()

    config_args = ["--config", args.config]
    run_all = not (args.fetch_only or args.process_only or args.sync_only)

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
