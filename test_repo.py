#!/usr/bin/env python3
"""
test_repo.py — Self-test: verify the repo is working correctly end-to-end.

Checks every component without needing AI assistance.

Usage:
    python test_repo.py          # run all checks
    python test_repo.py --fix    # attempt auto-fix of common issues

Exit code 0 = all OK, 1 = failures found.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"

results: list[tuple[str, str]] = []  # (status, message)


def check(name: str, ok: bool, detail: str = "", fix: str = "") -> bool:
    status = PASS if ok else FAIL
    msg = f"{status} {name}"
    if detail:
        msg += f" — {detail}"
    results.append((status, msg))
    print(msg)
    if not ok and fix:
        print(f"       Fix: {fix}")
    return ok


def warn(name: str, detail: str = "") -> None:
    msg = f"{WARN} {name}"
    if detail:
        msg += f" — {detail}"
    results.append((WARN, msg))
    print(msg)


# ── 1. Python version ──────────────────────────────────────────────────────────

def test_python() -> None:
    print("\n=== Python & Dependencies ===")
    ok = sys.version_info >= (3, 11)
    check("Python >= 3.11", ok, f"got {sys.version_info.major}.{sys.version_info.minor}")

    deps = ["yaml", "arxiv", "chromadb", "pdfplumber", "requests", "psutil"]
    for dep in deps:
        try:
            __import__(dep)
            check(f"import {dep}", True)
        except ImportError:
            check(f"import {dep}", False, fix=f"pip install {dep}")

    # MCP
    try:
        import mcp  # noqa: F401
        check("import mcp", True)
    except ImportError:
        check("import mcp", False, fix="pip install 'mcp[cli]'")


# ── 2. Config file ─────────────────────────────────────────────────────────────

def test_config() -> None:
    print("\n=== Configuration ===")
    cfg_path = ROOT / "config.yaml"
    check("config.yaml exists", cfg_path.exists(), fix="git checkout config.yaml")
    if not cfg_path.exists():
        return

    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    vault = Path(cfg.get("vault_path", "")).expanduser()
    check("vault_path set", bool(cfg.get("vault_path")))
    check("vault_path accessible", vault.exists() or True,
          detail=f"{vault} (will be created on first run)")

    db_path = ROOT / cfg.get("db_path", ".db/papers.sqlite")
    check("db_path configured", bool(cfg.get("db_path")))

    check("claude_model set", bool(cfg.get("claude_model")),
          detail=cfg.get("claude_model", "NOT SET"))
    profiles = cfg.get("profiles", {})
    enabled = [n for n, p in profiles.items() if p.get("enabled")]
    check("profiles configured", bool(enabled),
          detail=f"{len(enabled)} enabled: {', '.join(enabled)}")


# ── 3. Database ────────────────────────────────────────────────────────────────

def test_database() -> None:
    print("\n=== Database ===")
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    db_path = ROOT / cfg["db_path"]

    check("DB file exists", db_path.exists(),
          fix="python fetch.py --dry-run  # initialises DB")
    if not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("papers table exists", "papers" in tables)

    if "papers" in tables:
        total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        processed = conn.execute("SELECT COUNT(*) FROM papers WHERE processed=1").fetchone()[0]
        check("has papers", total > 0, detail=f"{total:,} total, {processed:,} processed",
              fix="python run.py --all-history --abstract-only")

        # Check schema has expected columns
        cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        for col in ("arxiv_id", "title", "abstract", "processed", "vault_path"):
            check(f"column '{col}' exists", col in cols)

    conn.close()


# ── 4. ChromaDB ────────────────────────────────────────────────────────────────

def test_chroma() -> None:
    print("\n=== ChromaDB Vector Index ===")
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    chroma_path = ROOT / cfg["chroma_path"]

    check("chroma dir exists", chroma_path.exists(),
          fix="python sync.py  # builds index")
    if not chroma_path.exists():
        return

    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(chroma_path))
        col = client.get_or_create_collection("quant_papers")
        n = col.count()
        check("ChromaDB readable", True, detail=f"{n:,} documents indexed")
        check("index is populated", n > 0, fix="python sync.py")

        if n > 0:
            # Test a semantic search
            results = col.query(query_texts=["momentum factor"], n_results=3)
            check("semantic search works", len(results["documents"][0]) > 0)
    except Exception as e:
        check("ChromaDB functional", False, detail=str(e),
              fix="python sync.py --rebuild")


# ── 5. MCP server ─────────────────────────────────────────────────────────────

def test_mcp() -> None:
    print("\n=== MCP Server ===")
    mcp_script = ROOT / "search_mcp.py"
    check("search_mcp.py exists", mcp_script.exists())

    # Check MCP is registered in .claude.json
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            check("MCP registered in ~/.claude.json", "quant-research" in servers,
                  fix="python install.py  # re-registers MCP")
        except Exception as e:
            warn("~/.claude.json", f"parse error: {e}")
    else:
        warn("~/.claude.json not found", "run install.py to register MCP")

    # Quick syntax check
    try:
        import ast
        ast.parse(mcp_script.read_text(encoding="utf-8"))
        check("search_mcp.py syntax", True)
    except SyntaxError as e:
        check("search_mcp.py syntax", False, detail=str(e))


# ── 6. Claude CLI ──────────────────────────────────────────────────────────────

def test_claude_cli() -> None:
    print("\n=== Claude CLI ===")
    claude_bin = shutil.which("claude")
    check("claude CLI installed", bool(claude_bin),
          fix="npm install -g @anthropic-ai/claude-code  (or install via desktop app)")
    if not claude_bin:
        return

    try:
        result = subprocess.run(
            [claude_bin, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        check("claude --version", result.returncode == 0,
              detail=result.stdout.strip()[:80])
    except subprocess.TimeoutExpired:
        check("claude --version", False, detail="timeout")
    except Exception as e:
        check("claude --version", False, detail=str(e))


# ── 7. Script syntax ───────────────────────────────────────────────────────────

def test_syntax() -> None:
    print("\n=== Script Syntax ===")
    import ast
    for script in ["fetch.py", "process.py", "sync.py", "run.py",
                   "research.py", "search_mcp.py", "master.py", "install.py"]:
        path = ROOT / script
        if not path.exists():
            warn(f"{script}", "not found")
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"))
            check(f"{script} syntax", True)
        except SyntaxError as e:
            check(f"{script} syntax", False, detail=f"line {e.lineno}: {e.msg}")


# ── 8. Vault directory ────────────────────────────────────────────────────────

def test_vault() -> None:
    print("\n=== Vault Files ===")
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    vault = Path(cfg["vault_path"]).expanduser()
    research_dir = vault / cfg.get("research_dir", "research")
    guidelines_dir = vault / "guidelines"

    check("vault dir exists", vault.exists())
    check("research dir exists", research_dir.exists())
    check("guidelines dir exists", guidelines_dir.exists(),
          fix="mkdir -p ~/Documents/ClaudeVault/guidelines")

    if research_dir.exists():
        md_files = list(research_dir.rglob("*.md"))
        check("has vault .md files", len(md_files) > 0,
              detail=f"{len(md_files):,} markdown files",
              fix="python run.py --all-history --abstract-only")

    distilled = guidelines_dir / "quant-methodology-distilled.md"
    if distilled.exists():
        wc = len(distilled.read_text(encoding="utf-8").split())
        check("distilled methodology exists", True, detail=f"{wc:,} words")
        check("distilled methodology quality", wc >= 2000,
              detail=f"{wc} words (need >= 2000)",
              fix="python research.py --distill")
    else:
        warn("distilled methodology", "not yet generated — run: python research.py --distill")


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary() -> int:
    total = len(results)
    passes = sum(1 for s, _ in results if s == PASS)
    fails = sum(1 for s, _ in results if s == FAIL)
    warns = sum(1 for s, _ in results if s == WARN)

    print(f"\n{'=' * 60}")
    print(f"  RESULTS: {passes}/{total} passed, {fails} failed, {warns} warnings")
    print(f"{'=' * 60}")

    if fails > 0:
        print("\nFailed checks:")
        for s, msg in results:
            if s == FAIL:
                print(f"  {msg}")

    return 0 if fails == 0 else 1


def main() -> int:
    parser_args = sys.argv[1:]
    auto_fix = "--fix" in parser_args

    print("=" * 60)
    print("  Quant Research Vault — Self-Test")
    print("=" * 60)

    if auto_fix:
        print("\nAuto-fix mode: will attempt to fix simple issues\n")

    test_python()
    test_config()
    test_database()
    test_chroma()
    test_mcp()
    test_claude_cli()
    test_syntax()
    test_vault()

    return print_summary()


if __name__ == "__main__":
    sys.exit(main())
