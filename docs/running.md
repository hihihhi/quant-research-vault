# Running the checks and the pipeline on Windows and macOS/Linux

Both blocks create a virtual environment, install the requirements plus the quality tools, run the same checks as CI
(`.github/workflows/quality.yml`), then a fetch dry run and the MCP server's help.

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt ruff mypy pytest
& .\.venv\Scripts\ruff.exe check .
& .\.venv\Scripts\ruff.exe format --check .
& .\.venv\Scripts\mypy.exe
& .\.venv\Scripts\pytest.exe -q
& .\.venv\Scripts\python.exe run.py --fetch-only --dry-run
& .\.venv\Scripts\python.exe search_mcp.py --help
```

```bash
# macOS/Linux (bash): the same steps
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt ruff mypy pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest -q
.venv/bin/python run.py --fetch-only --dry-run
.venv/bin/python search_mcp.py --help
```

The dry run makes live upstream requests but is intended not to persist fetched records. Run `python run.py --help` before any stateful ingestion command.
