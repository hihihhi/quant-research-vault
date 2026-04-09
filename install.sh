#!/usr/bin/env bash
# install.sh — Wire quant-research-vault into Claude Code
# Usage: ./install.sh

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_JSON="$HOME/.claude.json"

echo "=== quant-research-vault install ==="
echo "Repo: $REPO_DIR"

# ── 1. Install Python dependencies ───────────────────────────────────────────
echo ""
echo "Installing Python dependencies..."
if command -v uv &>/dev/null; then
    uv pip install -r "$REPO_DIR/requirements.txt"
else
    pip install -r "$REPO_DIR/requirements.txt"
fi

# ── 2. Create vault research directory ───────────────────────────────────────
VAULT_PATH=$(python3 -c "import yaml; print(yaml.safe_load(open('$REPO_DIR/config.yaml'))['vault_path'])")
RESEARCH_DIR="$VAULT_PATH/research"
mkdir -p "$RESEARCH_DIR"
echo "Vault research directory: $RESEARCH_DIR"

# ── 3. Wire MCP server into ~/.claude.json ────────────────────────────────────
echo ""
echo "Wiring quant-research MCP server into $CLAUDE_JSON..."

python3 << PYEOF
import json, pathlib, sys

claude_json = pathlib.Path("$CLAUDE_JSON")
repo_dir = "$REPO_DIR"
search_mcp = f"{repo_dir}/search_mcp.py"

if not claude_json.exists():
    print("ERROR: ~/.claude.json not found. Is Claude Code installed?")
    sys.exit(1)

d = json.loads(claude_json.read_text(encoding="utf-8"))
if "mcpServers" not in d:
    d["mcpServers"] = {}

if "quant-research" in d["mcpServers"]:
    print("quant-research MCP server already configured.")
else:
    import platform
    if platform.system() == "Windows":
        d["mcpServers"]["quant-research"] = {
            "type": "stdio",
            "command": "cmd",
            "args": ["/c", "python", search_mcp.replace("/", "\\\\")],
            "env": {}
        }
    else:
        d["mcpServers"]["quant-research"] = {
            "type": "stdio",
            "command": "python3",
            "args": [search_mcp],
            "env": {}
        }
    claude_json.write_text(json.dumps(d, indent=2), encoding="utf-8")
    print(f"Added quant-research MCP server -> {search_mcp}")
PYEOF

echo ""
echo "=== Install complete ==="
echo ""
echo "Next steps:"
echo "  1. Set ANTHROPIC_API_KEY in your environment"
echo "  2. Run initial fetch: python3 $REPO_DIR/run.py --days 30 --limit 10"
echo "  3. Restart Claude Code — mcp__quant-research__search_papers will be available"
echo ""
echo "To run daily (add to crontab):"
echo "  0 6 * * * cd $REPO_DIR && python3 run.py --limit 20 >> logs/daily.log 2>&1"
