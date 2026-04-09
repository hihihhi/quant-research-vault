# install.ps1 — Wire quant-research-vault into Claude Code (Windows)
# Usage: .\install.ps1

$ErrorActionPreference = "Stop"
$RepoDir = $PSScriptRoot
$ClaudeJson = "$env:USERPROFILE\.claude.json"

Write-Host "=== quant-research-vault install ===" -ForegroundColor Cyan
Write-Host "Repo: $RepoDir"

# ── 1. Install Python dependencies ───────────────────────────────────────────
Write-Host "`nInstalling Python dependencies..." -ForegroundColor Yellow
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    & uv pip install -r "$RepoDir\requirements.txt"
} else {
    & pip install -r "$RepoDir\requirements.txt"
}

# ── 2. Create vault research directory ───────────────────────────────────────
$vaultPath = python -c "import yaml; print(yaml.safe_load(open(r'$RepoDir\config.yaml'))['vault_path'])"
$researchDir = "$vaultPath\research"
New-Item -ItemType Directory -Force -Path $researchDir | Out-Null
Write-Host "Vault research directory: $researchDir"

# ── 3. Wire MCP server into ~/.claude.json ────────────────────────────────────
Write-Host "`nWiring quant-research MCP server into $ClaudeJson..." -ForegroundColor Yellow

python -c "
import json, pathlib, sys

claude_json = pathlib.Path(r'$ClaudeJson')
search_mcp = r'$RepoDir\search_mcp.py'

if not claude_json.exists():
    print('ERROR: ~/.claude.json not found. Is Claude Code installed?')
    sys.exit(1)

d = json.loads(claude_json.read_text(encoding='utf-8'))
if 'mcpServers' not in d:
    d['mcpServers'] = {}

if 'quant-research' in d['mcpServers']:
    print('quant-research MCP server already configured.')
else:
    d['mcpServers']['quant-research'] = {
        'type': 'stdio',
        'command': 'cmd',
        'args': ['/c', 'python', search_mcp],
        'env': {}
    }
    claude_json.write_text(json.dumps(d, indent=2), encoding='utf-8')
    print(f'Added quant-research MCP server -> {search_mcp}')
"

Write-Host ""
Write-Host "=== Install complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Set ANTHROPIC_API_KEY in your environment (see .env.example)"
Write-Host "  2. Run initial fetch: python $RepoDir\run.py --days 30 --limit 10"
Write-Host "  3. Restart Claude Code -- mcp__quant-research__search_papers will be available"
