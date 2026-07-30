from __future__ import annotations

from datetime import date

import fetch
import search_mcp


def test_save_paper_deduplicates_arxiv_id(tmp_path):
    conn = fetch.init_db(str(tmp_path / "papers.sqlite"))
    paper = {
        "arxiv_id": "2401.00001",
        "title": "Example",
        "authors": ["A. Researcher"],
        "abstract": "A test paper.",
        "categories": ["q-fin.ST"],
        "published": "2024-01-01T00:00:00+00:00",
        "pdf_url": "https://example.test/paper.pdf",
    }
    fetch.save_paper(conn, paper)
    fetch.save_paper(conn, paper)
    assert fetch.already_fetched(conn, "2401.00001")
    assert fetch.count_total(conn) == 1
    conn.close()


def test_date_windows_cover_range_without_overlap():
    assert fetch.date_windows(date(2024, 1, 1), date(2024, 8, 1), chunk_months=3) == [
        (date(2024, 1, 1), date(2024, 4, 1)),
        (date(2024, 4, 1), date(2024, 7, 1)),
        (date(2024, 7, 1), date(2024, 8, 1)),
    ]


def test_retry_uses_exponential_backoff_for_rate_limit(monkeypatch):
    class FakeHttpError(Exception):
        def __init__(self, status: int):
            self.status = status

    class Client:
        def __init__(self):
            self.calls = 0

        def results(self, _search):
            self.calls += 1
            if self.calls < 3:
                raise FakeHttpError(429)
            return ["paper"]

    pauses: list[int] = []
    monkeypatch.setattr(fetch.arxiv, "HTTPError", FakeHttpError)
    monkeypatch.setattr("time.sleep", pauses.append)
    assert fetch._iter_with_retry(Client(), object()) == ["paper"]
    assert pauses == [60, 120]


def test_single_instance_lock_rejects_live_owner(monkeypatch, tmp_path):
    lock = tmp_path / "quant_research_mcp.lock"
    monkeypatch.setattr(search_mcp, "_LOCK_FILE", lock)
    monkeypatch.setattr(search_mcp, "_is_pid_alive", lambda pid: pid == 123)
    lock.write_text("123", encoding="utf-8")
    assert not search_mcp._acquire_lock()
    lock.write_text("999", encoding="utf-8")
    assert search_mcp._acquire_lock()
    assert lock.read_text(encoding="utf-8")
    search_mcp._release_lock()
    assert not lock.exists()


def test_search_papers_maps_chroma_response():
    class Collection:
        def count(self):
            return 1

        def query(self, **_kwargs):
            return {
                "documents": [["Abstract"]],
                "metadatas": [[{"arxiv_id": "2401.00001", "title": "Example"}]],
                "distances": [[0.1]],
            }

    assert search_mcp.search_papers(Collection(), "momentum") == [
        {
            "arxiv_id": "2401.00001",
            "title": "Example",
            "categories": None,
            "published": None,
            "relevance_score": 0.9,
            "vault_path": None,
            "summary_excerpt": "Abstract",
        }
    ]
