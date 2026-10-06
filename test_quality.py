from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

import fetch
import install
import master
import process
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


def test_arxiv_versions_map_to_one_id():
    for entry_id in (
        "http://arxiv.org/abs/2401.12345",
        "http://arxiv.org/abs/2401.12345v1",
        "http://arxiv.org/abs/2401.12345v12",
    ):
        assert fetch.arxiv_id_from_entry(entry_id) == "2401.12345"
    assert fetch.arxiv_id_from_entry("http://arxiv.org/abs/q-fin/0501001v2") == (
        "q-fin/0501001"
    )


def test_revised_paper_is_stored_once(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    def result(version: str):
        return SimpleNamespace(
            entry_id=f"http://arxiv.org/abs/2401.12345{version}",
            title="Example",
            authors=[],
            summary="A test paper.",
            categories=["q-fin.ST"],
            published=datetime(2024, 1, 1, tzinfo=timezone.utc),
            pdf_url="https://example.test/paper.pdf",
        )

    conn = fetch.init_db(str(tmp_path / "papers.sqlite"))
    fetch.save_paper(conn, fetch._to_dict(result("v1")))
    fetch.save_paper(conn, fetch._to_dict(result("v2")))
    assert fetch.count_total(conn) == 1
    assert fetch.already_fetched(conn, "2401.12345")
    conn.close()


def _processing_setup(monkeypatch, tmp_path, n_papers):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f'vault_path: "{tmp_path / "vault"}"\nresearch_dir: research\n'
        f'db_path: "{tmp_path / "papers.sqlite"}"\nclaude_model: m\n'
        "max_pdf_chars: 100\nfetch_pdf: false\n",
        encoding="utf-8",
    )
    conn = fetch.init_db(str(tmp_path / "papers.sqlite"))
    for i in range(n_papers):
        fetch.save_paper(
            conn,
            {
                "arxiv_id": f"2401.0000{i}",
                "title": "Example",
                "authors": ["A"],
                "abstract": "A test paper.",
                "categories": ["q-fin.ST"],
                "published": "2024-01-01T00:00:00+00:00",
                "pdf_url": "https://example.test/p.pdf",
            },
        )
    conn.close()
    monkeypatch.setattr(
        sys, "argv", ["process.py", "--config", str(cfg_path), "--workers", "1"]
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_full_analysis_exits_nonzero_when_every_paper_fails(monkeypatch, tmp_path):
    _processing_setup(monkeypatch, tmp_path, 2)
    with pytest.raises(SystemExit) as exc:
        process.main()  # no API key: summarize() raises for every paper
    assert exc.value.code not in (0, None)


def test_abstract_only_run_still_exits_zero(monkeypatch, tmp_path):
    _processing_setup(monkeypatch, tmp_path, 2)
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--abstract-only"])
    process.main()  # returns normally: exit status 0


def _master_in(monkeypatch, tmp_path, config_text=None):
    real = Path(__file__).parent / "config.yaml"
    (tmp_path / "config.yaml").write_text(
        config_text if config_text is not None else real.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(master, "ROOT", tmp_path)
    monkeypatch.setattr(master, "PROGRESS_FILE", tmp_path / ".db" / "p.json")
    monkeypatch.setattr(master, "LOG_FILE", tmp_path / "master.log")
    monkeypatch.setattr(master, "paper_count", lambda: 0)
    return dict(master._DEFAULT_PROGRESS)


def _ss_enabled(tmp_path):
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    return cfg["extra_sources"]["semantic_scholar"]["enabled"]


def test_master_enables_semantic_scholar_for_the_step_then_restores(
    monkeypatch, tmp_path
):
    progress = _master_in(monkeypatch, tmp_path)
    assert _ss_enabled(tmp_path) is False  # the shipped config.yaml line
    seen = []

    def fake_run_step(cmd, timeout=0, label=""):
        seen.append(_ss_enabled(tmp_path))
        return 0

    monkeypatch.setattr(master, "run_step", fake_run_step)
    assert master.phase3_semantic_scholar(progress)
    assert seen == [True]  # fetch.py --fetch-ss would have seen enabled: true
    assert _ss_enabled(tmp_path) is False
    assert progress["ss_done"] is True


def test_master_does_not_mark_semantic_scholar_done_when_step_fails(
    monkeypatch, tmp_path
):
    progress = _master_in(monkeypatch, tmp_path)
    monkeypatch.setattr(master, "run_step", lambda *a, **k: 1)
    with pytest.raises(SystemExit):
        master.phase3_semantic_scholar(progress)
    assert progress["ss_done"] is False
    assert _ss_enabled(tmp_path) is False  # config restored even on failure


def test_master_refuses_to_mark_done_when_config_key_is_missing(monkeypatch, tmp_path):
    progress = _master_in(monkeypatch, tmp_path, "extra_sources: {}\n")
    monkeypatch.setattr(master, "run_step", lambda *a, **k: 0)
    with pytest.raises(SystemExit):
        master.phase3_semantic_scholar(progress)
    assert progress["ss_done"] is False


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_install_sh_expands_tilde_in_vault_path(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    stubs = tmp_path / "stubs"
    for d in (home, work, stubs):
        d.mkdir()
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    pip = stubs / "pip"
    pip.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pip.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{stubs}:{Path(sys.executable).parent}:/usr/bin:/bin",
    }
    root = Path(__file__).parent
    result = subprocess.run(
        ["bash", str(root / "install.sh")],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (home / "Documents" / "ClaudeVault" / "research").is_dir()
    assert not (work / "~").exists()


def _broken_md_links(root: Path) -> list[str]:
    import re

    broken = []
    for md in sorted(root.rglob("*.md")):
        if any(part in {".git", ".venv", "node_modules"} for part in md.parts):
            continue
        text = re.sub(r"```.*?```", "", md.read_text(encoding="utf-8"), flags=re.S)
        for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", text):
            if re.match(r"[a-z][a-z0-9+.-]*:|#", target, re.I):
                continue  # external URL or same-page anchor
            if not (md.parent / target.split("#")[0]).exists():
                broken.append(f"{md.relative_to(root)} -> {target}")
    return broken


def test_markdown_link_check_flags_a_missing_file(tmp_path):
    (tmp_path / "a.md").write_text("[ok](b.md) [bad](nope.md#x)", encoding="utf-8")
    (tmp_path / "b.md").write_text("[web](https://example.test) [top](#t)")
    assert _broken_md_links(tmp_path) == ["a.md -> nope.md#x"]


def test_repo_markdown_relative_links_resolve():
    assert _broken_md_links(Path(__file__).parent) == []
