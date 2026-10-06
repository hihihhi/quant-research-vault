from __future__ import annotations

import json
from datetime import date

import pytest

import fetch
import install
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


def test_mcp_db_paths_resolve_next_to_config_not_cwd(monkeypatch, tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "config.yaml").write_text(
        'vault_path: "~/v"\ndb_path: ".db/papers.sqlite"\nchroma_path: ".db/chroma"\n',
        encoding="utf-8",
    )
    (pkg / ".db").mkdir()
    fetch.init_db(str(pkg / ".db" / "papers.sqlite")).close()
    elsewhere = tmp_path / "user-project"
    elsewhere.mkdir()
    monkeypatch.setattr(search_mcp, "CONFIG_PATH", pkg / "config.yaml")
    monkeypatch.chdir(elsewhere)

    cfg = search_mcp.load_config()
    conn = search_mcp.get_db(cfg)
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone() == (0,)
    conn.close()
    assert cfg["chroma_path"] == str(pkg / ".db" / "chroma")
    assert not (elsewhere / ".db").exists()


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path / ".claude.json"


def test_installer_refuses_malformed_claude_json_and_keeps_a_backup(
    monkeypatch, tmp_path
):
    claude_json = _home(monkeypatch, tmp_path)
    broken = '{"mcpServers": {"other": {"command": "x"}}, "projects": {},}'
    claude_json.write_text(broken, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        install.wire_mcp({"mcp_server_name": "quant-research"})

    assert exc.value.code != 0
    assert claude_json.read_text(encoding="utf-8") == broken
    backups = list(tmp_path.glob(".claude.json.malformed-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == broken


def test_installer_adds_server_and_keeps_existing_claude_json_settings(
    monkeypatch, tmp_path
):
    claude_json = _home(monkeypatch, tmp_path)
    claude_json.write_text(
        '{"theme": "dark", "mcpServers": {"other": {"command": "x"}}}',
        encoding="utf-8",
    )
    install.wire_mcp({"mcp_server_name": "quant-research"})
    data = json.loads(claude_json.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert set(data["mcpServers"]) == {"other", "quant-research"}
