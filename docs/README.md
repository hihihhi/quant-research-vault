# Quant Research Vault documentation

## Understand it

| Page | What it answers |
| --- | --- |
| [architecture.md](architecture.md) | What does each stage do, how do the arXiv retries back off, and what happens in one MCP search call? |
| [analysis-skill.md](analysis-skill.md) | How does Claude Code run the optional full-paper analysis over pending vault entries? (The instructions a user points Claude Code at; `process.py` and `master.py` refer to it.) |

## Check the evidence

| Page | What it answers |
| --- | --- |
| [corpus-audit.md](corpus-audit.md) | What does the 18,492-row corpus count rest on, and what does it not show? |
| [corpus_audit_snapshot_2026-07-30.json](corpus_audit_snapshot_2026-07-30.json) | The committed query results and SQLite file hash behind that count. |

## Reference

| Page | What it answers |
| --- | --- |
| [running.md](running.md) | How do I install, run the CI checks, do a fetch dry run and start the MCP server on Windows PowerShell or macOS/Linux? |
