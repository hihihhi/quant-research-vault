#!/usr/bin/env python3
"""
master.py — Autonomous orchestration for full quant brain build.

Phases (in order, each restartable):
  1. Wait for arXiv all-history abstract-only to finish
  2. Run OpenAlex windowed fetch (non-arXiv papers)
  3. Run Semantic Scholar bulk import
  4. Sync ChromaDB after all sources
  5. Run Phase 2 (full Claude analysis) in batches of 100
  6. Distill insights at milestones (first run, 5k papers, 10k, each 2k Phase2 papers)
  7. Verify distillation quality; re-run if thin

State: .db/master_progress.json
Log: master.log

Usage:
    python master.py           # run full pipeline to completion
    python master.py --status  # show progress without running
    python master.py --distill-now  # trigger distillation immediately
"""

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
PROGRESS_FILE = ROOT / ".db" / "master_progress.json"
LOG_FILE = ROOT / "master.log"

_DEFAULT_PROGRESS = {
    "arxiv_done": False,
    "openalex_done": False,
    "ss_done": False,
    "synced_after_sources": False,
    "first_distill_done": False,
    "distillations": [],
    "last_distill_paper_count": 0,
    "verified": False,
    "started_at": None,
    "last_updated": None,
}

_active_proc: "subprocess.Popen | None" = None


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Progress ──────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return {**_DEFAULT_PROGRESS, **json.loads(PROGRESS_FILE.read_text())}
        except Exception:
            pass
    return dict(_DEFAULT_PROGRESS)


def save_progress(p: dict) -> None:
    p["last_updated"] = datetime.now(timezone.utc).isoformat()
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(p, indent=2))


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["vault_path"] = str(Path(cfg["vault_path"]).expanduser())
    return cfg


def paper_count() -> int:
    cfg = load_config()
    try:
        conn = sqlite3.connect(cfg["db_path"])
        n = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def pending_count() -> int:
    """Papers not yet processed at all (processed=0)."""
    cfg = load_config()
    try:
        conn = sqlite3.connect(cfg["db_path"])
        n = conn.execute("SELECT COUNT(*) FROM papers WHERE processed=0").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


# ── Subprocess runner ─────────────────────────────────────────────────────────

def run_step(cmd: list[str], timeout: int = 7200, label: str = "") -> int:
    global _active_proc
    log(f"Running: {' '.join(cmd)}" + (f" [{label}]" if label else ""))
    try:
        _active_proc = subprocess.Popen(cmd, cwd=ROOT)
        _active_proc.wait(timeout=timeout)
        rc = _active_proc.returncode
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT after {timeout}s — killing")
        _active_proc.kill()
        _active_proc.wait()
        rc = 1
    finally:
        _active_proc = None
    log(f"Finished (rc={rc}): {label or cmd[-1]}")
    return rc


# ── Phase 1: Wait for arXiv download ─────────────────────────────────────────

def arxiv_is_done() -> bool:
    """Check all-history.log for completion marker."""
    log_file = ROOT / "all-history.log"
    if not log_file.exists():
        return False
    content = log_file.read_text(encoding="utf-8", errors="replace")
    return "All-history pipeline complete." in content


def phase1_wait(progress: dict) -> bool:
    if progress["arxiv_done"]:
        return True
    if arxiv_is_done():
        log("Phase 1 (arXiv) complete — detected in all-history.log")
        progress["arxiv_done"] = True
        save_progress(progress)
        return True
    n = paper_count()
    log(f"Phase 1 still running — {n} papers so far. Waiting...")
    return False


# ── Phase 2: OpenAlex fetch ───────────────────────────────────────────────────

def phase2_openalex(progress: dict) -> bool:
    if progress["openalex_done"]:
        return True

    # Temporarily enable OpenAlex in config
    cfg = load_config()
    cfg_path = ROOT / "config.yaml"
    raw = cfg_path.read_text(encoding="utf-8")
    if "enabled: false          # integrated into --all-history windowed fetch" in raw:
        raw_oa = raw.replace(
            "enabled: false          # integrated into --all-history windowed fetch",
            "enabled: true           # integrated into --all-history windowed fetch",
        )
        cfg_path.write_text(raw_oa, encoding="utf-8")
        log("OpenAlex enabled in config.yaml")
        oa_enabled = True
    else:
        oa_enabled = False
        log("OpenAlex already enabled or config format changed — proceeding")

    before = paper_count()
    rc = run_step(
        [sys.executable, str(ROOT / "run.py"), "--all-history", "--abstract-only"],
        timeout=7200,
        label="OpenAlex all-history",
    )

    # Restore config: disable OpenAlex
    if oa_enabled:
        raw2 = cfg_path.read_text(encoding="utf-8")
        raw2 = raw2.replace(
            "enabled: true           # integrated into --all-history windowed fetch",
            "enabled: false          # integrated into --all-history windowed fetch",
        )
        cfg_path.write_text(raw2, encoding="utf-8")
        log("OpenAlex disabled in config.yaml (restored)")

    after = paper_count()
    log(f"OpenAlex fetch done. Added {after - before} papers. DB: {after}")
    progress["openalex_done"] = True
    save_progress(progress)
    return True


# ── Phase 3: Semantic Scholar ─────────────────────────────────────────────────

def phase3_semantic_scholar(progress: dict) -> bool:
    if progress["ss_done"]:
        return True

    # Temporarily enable SS in config
    cfg_path = ROOT / "config.yaml"
    raw = cfg_path.read_text(encoding="utf-8")
    if "enabled: false          # run separately: python fetch.py --fetch-ss" in raw:
        raw_ss = raw.replace(
            "enabled: false          # run separately: python fetch.py --fetch-ss",
            "enabled: true           # run separately: python fetch.py --fetch-ss",
        )
        cfg_path.write_text(raw_ss, encoding="utf-8")
        log("Semantic Scholar enabled in config.yaml")
        ss_enabled = True
    else:
        ss_enabled = False
        log("Semantic Scholar already enabled or config format changed")

    before = paper_count()
    rc = run_step(
        [sys.executable, str(ROOT / "fetch.py"), "--fetch-ss"],
        timeout=7200,
        label="Semantic Scholar bulk import",
    )

    # Restore config
    if ss_enabled:
        raw2 = cfg_path.read_text(encoding="utf-8")
        raw2 = raw2.replace(
            "enabled: true           # run separately: python fetch.py --fetch-ss",
            "enabled: false          # run separately: python fetch.py --fetch-ss",
        )
        cfg_path.write_text(raw2, encoding="utf-8")
        log("Semantic Scholar disabled in config.yaml (restored)")

    after = paper_count()
    log(f"SS import done. Added {after - before} papers. DB: {after}")
    progress["ss_done"] = True
    save_progress(progress)
    return True


# ── Phase 4: Sync ChromaDB ────────────────────────────────────────────────────

def phase4_sync(progress: dict, label: str = "") -> None:
    rc = run_step(
        [sys.executable, str(ROOT / "sync.py")],
        timeout=900,
        label=f"sync ChromaDB {label}",
    )
    if rc == 0:
        progress["sync_count"] = progress.get("sync_count", 0) + 1
        save_progress(progress)


# ── Phase 5: Distillation ─────────────────────────────────────────────────────

def run_distillation(progress: dict, label: str = "") -> bool:
    """Run full 12-topic distillation. Returns True on success."""
    n = paper_count()
    log(f"Starting distillation ({label}) — {n} papers in vault")

    rc = run_step(
        [sys.executable, str(ROOT / "research.py"), "--distill"],
        timeout=3600,
        label=f"distill {label}",
    )

    if rc == 0:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "paper_count": n,
            "label": label,
        }
        progress["distillations"].append(entry)
        progress["last_distill_paper_count"] = n
        progress["first_distill_done"] = True
        save_progress(progress)
        log(f"Distillation complete ({label}) — {n} papers")
        return True
    log(f"Distillation FAILED (rc={rc})")
    return False


def should_distill(progress: dict) -> bool:
    """Check if a new distillation pass is warranted."""
    if not progress["first_distill_done"]:
        return True

    # Re-distill when paper count grows by 25% or 2000 papers (whichever first)
    n = paper_count()
    last = progress.get("last_distill_paper_count", 0)
    growth = n - last
    if growth >= max(2000, int(last * 0.25)):
        return True

    return False


# ── Phase 7: Verification ─────────────────────────────────────────────────────

def verify_distillation() -> dict:
    """Check the quality of the most recent distillation output."""
    cfg = load_config()
    out_file = Path(cfg["vault_path"]) / "guidelines" / "quant-methodology-distilled.md"

    if not out_file.exists():
        return {"ok": False, "reason": "output file not found"}

    content = out_file.read_text(encoding="utf-8")
    word_count = len(content.split())
    section_count = content.count("\n## ")
    arxiv_refs = len([l for l in content.split("\n") if "[" in l and "]" in l and "." in l])

    required_topics = [
        "momentum", "mean reversion", "volatility", "factor", "crypto",
        "backtesting", "overfitting", "microstructure",
    ]
    covered = sum(1 for t in required_topics if t.lower() in content.lower())

    ok = (word_count >= 3000 and section_count >= 8 and covered >= 6)
    return {
        "ok": ok,
        "word_count": word_count,
        "sections": section_count,
        "arxiv_refs": arxiv_refs,
        "topics_covered": f"{covered}/{len(required_topics)}",
    }


# ── Status display ────────────────────────────────────────────────────────────

def print_status(progress: dict) -> None:
    n = paper_count()
    print("\n" + "=" * 60)
    print("  QUANT BRAIN BUILD — STATUS")
    print("=" * 60)
    print(f"  Papers in DB     : {n:,}")
    print(f"  Phase 1 arXiv    : {'DONE' if progress['arxiv_done'] else 'IN PROGRESS'}")
    print(f"  Phase 2 OpenAlex : {'DONE' if progress['openalex_done'] else 'PENDING'}")
    print(f"  Phase 3 Sem.Sch. : {'DONE' if progress['ss_done'] else 'PENDING'}")
    print(f"  Distillations    : {len(progress['distillations'])} runs")
    if progress["distillations"]:
        last = progress["distillations"][-1]
        print(f"  Last distill     : {last['timestamp'][:10]} ({last['paper_count']:,} papers)")
    vr = verify_distillation()
    print(f"  Distill quality  : {'OK' if vr.get('ok') else 'NEEDS WORK'} — "
          f"{vr.get('word_count', 0):,} words, {vr.get('topics_covered', '?')} topics")
    print(f"\n  Full analysis    : use Claude Code + ANALYSIS_SKILL.md")
    print("=" * 60)


# ── Signal handler ────────────────────────────────────────────────────────────

def _install_sigint() -> None:
    def _handler(sig, frame):  # noqa: ANN001
        print("\n[master.py] Ctrl+C — stopping. Progress saved. Re-run to continue.", flush=True)
        if _active_proc and _active_proc.poll() is None:
            _active_proc.terminate()
            try:
                _active_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _active_proc.kill()
        sys.exit(0)
    signal.signal(signal.SIGINT, _handler)


# ── Main orchestration loop ───────────────────────────────────────────────────

def main() -> None:
    _install_sigint()

    parser = argparse.ArgumentParser(description="Quant Brain autonomous build orchestration")
    parser.add_argument("--status", action="store_true", help="Show status and exit")
    parser.add_argument("--distill-now", action="store_true", help="Run distillation immediately")
    args = parser.parse_args()

    progress = load_progress()
    if progress["started_at"] is None:
        progress["started_at"] = datetime.now(timezone.utc).isoformat()
        save_progress(progress)

    if args.status:
        print_status(progress)
        return

    if args.distill_now:
        run_distillation(progress, label="manual")
        vr = verify_distillation()
        log(f"Verification: {vr}")
        return

    log("=" * 60)
    log("QUANT BRAIN BUILD — starting orchestration")
    log(f"Paper count: {paper_count():,}")
    log("=" * 60)

    # ── Full orchestration loop ───────────────────────────────────────────────
    iteration = 0
    while True:
        iteration += 1
        log(f"--- Orchestration loop iteration {iteration} ---")

        # Step 1: Wait for arXiv download
        if not progress["arxiv_done"]:
            done = phase1_wait(progress)
            if not done:
                log("Waiting 60s for arXiv download to progress...")
                time.sleep(60)
                continue

        # Step 2: OpenAlex
        if not progress["openalex_done"]:
            log("Starting OpenAlex fetch...")
            phase2_openalex(progress)
            phase4_sync(progress, "after-openalex")
            time.sleep(5)
            continue

        # Step 3: Semantic Scholar
        if not progress["ss_done"]:
            log("Starting Semantic Scholar import...")
            phase3_semantic_scholar(progress)
            phase4_sync(progress, "after-ss")
            time.sleep(5)
            continue

        # Step 4: First distillation (after all sources loaded)
        if not progress["first_distill_done"]:
            log("Running first distillation...")
            run_distillation(progress, label="initial")
            vr = verify_distillation()
            log(f"Verification: {vr}")
            time.sleep(5)
            continue

        # Step 5: All sources downloaded and distilled — done.
        log("All download phases complete!")
        vr = verify_distillation()
        log(f"Distillation quality: {vr}")

        if vr.get("ok"):
            progress["verified"] = True
            save_progress(progress)
            log("COMPLETE — papers downloaded and distilled. Use ANALYSIS_SKILL.md for full analysis.")
            print_status(progress)
        else:
            log(f"Quality check FAILED: {vr}. Re-running distillation...")
            run_distillation(progress, label=f"retry-{len(progress['distillations'])}")
            vr2 = verify_distillation()
            if vr2.get("ok"):
                progress["verified"] = True
                save_progress(progress)
                log("COMPLETE after retry — quant brain ready.")
            else:
                log("Distillation quality still thin. Add more papers or run --distill-now manually.")
        break

    log("master.py done.")


if __name__ == "__main__":
    main()
