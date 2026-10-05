# Corpus audit: what the 18,492-row count rests on

The [audit snapshot](corpus_audit_snapshot_2026-07-30.json) records
`SELECT count(*) FROM papers = 18492` against an ignored 33,161,216-byte SQLite file with SHA-256
`acb1a76ee2bf575fa083c807be39b66b34c040a6b9697a297b1e0dc0a0e7ab13`; the underlying rows are not published.

- The count is an operational row count at the 2026-07-30 audit, not a quality or model-performance result, and its
  sample status is unknown.
- The code's `processed` flag means an entry was written, not that analysis is complete.
- The corpus, SQLite database and ChromaDB index are not published, so the count, coverage and retrieval quality are not
  independently reproducible from this checkout.
