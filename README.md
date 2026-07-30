# Quant Research Vault

## One sentence
A local research pipeline that collects selected academic-paper metadata, organizes it for retrieval, and supports optional model-assisted analysis.

## Result
No headline result. IS performance: unknown. OOS performance: unknown. The committed public source contains pipeline code and configuration, but no reproducible public corpus, benchmark, backtest, or performance metric; therefore no research or trading result is claimed here. Source status: arXiv and OpenAlex are enabled in the committed configuration; Semantic Scholar is configured but disabled by default. The public checkout includes no paper sample or retained query output.

## How it works
- `fetch.py` retrieves configured metadata.
- `process.py` writes local paper records.
- `sync.py` builds a local ChromaDB index.
- `research.py` exposes CLI retrieval and synthesis commands.
- `run.py` and `master.py` coordinate the stages; source availability and coverage depend on upstream APIs and the selected profile.

## The interesting decision
The pipeline separates early abstract-only indexing from later model-assisted enrichment, making a local corpus searchable before optional full-text/model analysis. This is a workflow choice, not evidence of signal quality, economic value, or live-trading suitability.

## Run it
```text
python -m pip install -r requirements.txt
python test_repo.py
python run.py --all-history --abstract-only
python research.py --stats
```
The retrieval command is operator-initiated and may call upstream APIs; API access, rate limits, optional credentials, and generated local state are environment-dependent. Do not commit generated data, keys, or local endpoints.

## Status
Active public-source preparation. Any generated index, database, PDFs, exports, model outputs, and methodology artifacts are local artifacts whose provenance is not established by this repository checkout. Validate source records, licenses, transformations, and experimental methodology independently before relying on any output.
