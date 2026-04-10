#!/usr/bin/env python3
"""
install.py — One-command setup for quant-research-vault.

What it does:
  1. Checks Python >= 3.11
  2. pip install -r requirements.txt
  3. Creates vault directory tree
  4. Initialises SQLite DB
  5. Wires MCP server into ~/.claude.json
  6. Creates Windows Task Scheduler daily job

Usage:
    python install.py
"""

import json
import subprocess
import sys
from pathlib import Path

import yaml

# ── UTF-8 stdout fix ──────────────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent


def load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["vault_path"] = str(Path(cfg["vault_path"]).expanduser())
    return cfg


def step(msg: str) -> None:
    print(f"\n>>> {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"    OK  {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"    WARN {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"    FAIL {msg}", flush=True)
    sys.exit(1)


# ── 1. Python version check ───────────────────────────────────────────────────

def check_python() -> None:
    step("Checking Python version...")
    if sys.version_info < (3, 11):
        fail(f"Python >= 3.11 required. You have {sys.version}. Please upgrade.")
    ok(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


# ── 2. pip install ────────────────────────────────────────────────────────────

def install_requirements() -> None:
    step("Installing requirements...")
    req_path = ROOT / "requirements.txt"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req_path)],
        capture_output=False,
    )
    if result.returncode != 0:
        fail("pip install failed. Check output above.")
    ok("All packages installed.")


# ── 3. Create vault directory tree ───────────────────────────────────────────

def create_vault_dirs(cfg: dict) -> None:
    step("Creating vault directory tree...")
    vault_path = Path(cfg["vault_path"])
    research_dir = vault_path / cfg.get("research_dir", "research")

    research_dir.mkdir(parents=True, exist_ok=True)
    for profile_name, profile in cfg.get("profiles", {}).items():
        if not profile.get("enabled"):
            continue
        for cat in profile.get("categories", []):
            (research_dir / cat).mkdir(parents=True, exist_ok=True)

    # Also create .db directory
    db_path = Path(cfg["db_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    chroma_path = Path(cfg["chroma_path"])
    chroma_path.mkdir(parents=True, exist_ok=True)

    ok(f"Vault: {vault_path}")
    ok(f"Research dir: {research_dir}")
    ok(f"DB dir: {db_path.parent}")
    ok(f"Chroma dir: {chroma_path}")


# ── 4. Initialise SQLite DB ───────────────────────────────────────────────────

def init_db(cfg: dict) -> None:
    step("Initialising SQLite database...")
    db_path = cfg["db_path"]
    result = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, r'{ROOT}'); "
         f"import fetch; fetch.init_db(r'{db_path}'); "
         f"print('DB initialised at {db_path}')"],
        capture_output=False,
    )
    if result.returncode != 0:
        fail("DB init failed.")
    ok(f"SQLite DB ready at {db_path}")


# ── 5. Wire MCP server into ~/.claude.json ────────────────────────────────────

def wire_mcp(cfg: dict) -> None:
    step("Wiring MCP server into ~/.claude.json...")
    claude_json_path = Path.home() / ".claude.json"
    server_name = cfg.get("mcp_server_name", "quant-research")
    script_path = ROOT / "search_mcp.py"

    # Load existing .claude.json or start fresh
    if claude_json_path.exists():
        try:
            data = json.loads(claude_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            warn(".claude.json is malformed — creating a new one.")
            data = {}
    else:
        data = {}

    if "mcpServers" not in data:
        data["mcpServers"] = {}

    script_str = str(script_path).replace("\\", "/")

    if sys.platform == "win32":
        server_config = {
            "type": "stdio",
            "command": "cmd",
            "args": ["/c", "python", script_str],
            "env": {},
        }
    else:
        import shutil as _shutil
        python_bin = _shutil.which("python3") or _shutil.which("python") or "python3"
        server_config = {
            "type": "stdio",
            "command": python_bin,
            "args": [script_str],
            "env": {},
        }

    data["mcpServers"][server_name] = server_config

    claude_json_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    ok(f"MCP server '{server_name}' registered in {claude_json_path}")


# ── 6. Windows Task Scheduler daily job ──────────────────────────────────────

def create_scheduled_task() -> None:
    if sys.platform != "win32":
        step("Daily job (non-Windows)...")
        run_script = ROOT / "run.py"
        import shutil as _shutil
        python_bin = _shutil.which("python3") or "python3"
        print(f"    Add to crontab (run: crontab -e):")
        print(f"    0 6 * * * cd {ROOT} && {python_bin} {run_script}")
        return

    step("Creating Windows Task Scheduler daily job...")
    run_script = str(ROOT / "run.py").replace("/", "\\")
    python_exe = str(Path(sys.executable)).replace("/", "\\")
    task_cmd = f'"{python_exe}" "{run_script}"'

    result = subprocess.run(
        [
            "schtasks", "/create",
            "/tn", "QuantResearchVault-Daily",
            "/tr", task_cmd,
            "/sc", "daily",
            "/st", "06:00",
            "/f",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        warn(f"Task Scheduler setup failed (may need admin rights): {result.stderr.strip()}")
        warn("You can run this manually: schtasks /create /tn QuantResearchVault-Daily "
             f'/tr "{task_cmd}" /sc daily /st 06:00 /f')
    else:
        ok("Scheduled task 'QuantResearchVault-Daily' created (runs daily at 06:00).")


# ── What to do next ───────────────────────────────────────────────────────────

def print_next_steps() -> None:
    print("\n" + "=" * 60)
    print("  INSTALL COMPLETE — What to do next:")
    print("=" * 60)
    print()
    print("  Phase 1: Index all ~40,000 papers via abstracts (~2 hours)")
    print("  ┌──────────────────────────────────────────────────────────┐")
    print("  │  python run.py --all-history --abstract-only            │")
    print("  └──────────────────────────────────────────────────────────┘")
    print()
    print("  Phase 2: Full Claude analysis, 3 parallel workers (~2-3 days)")
    print("  ┌──────────────────────────────────────────────────────────┐")
    print("  │  python run.py --all-history --workers 3                │")
    print("  └──────────────────────────────────────────────────────────┘")
    print()
    print("  After Phase 1, Claude Code can already search your vault:")
    print("    search_papers('momentum factor crypto')")
    print("    list_recent_papers(7)")
    print("    get_paper('2401.12345')")
    print()
    print("  CLI research:")
    print("    python research.py 'volatility forecasting'")
    print("    python research.py --stats")
    print("    python research.py --alpha-ideas 'crypto momentum'")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  Quant Research Vault — Installer")
    print("=" * 60)

    check_python()
    cfg = load_config()
    install_requirements()
    create_vault_dirs(cfg)
    init_db(cfg)
    wire_mcp(cfg)
    create_scheduled_task()
    print_next_steps()


if __name__ == "__main__":
    main()
