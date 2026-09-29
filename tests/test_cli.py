from pathlib import Path
import sqlite3
import tomllib

import pytest

import gaohe
import gaohe.cli as cli
from gaohe.cli import main
from gaohe.config import load_settings
from gaohe.domain import ArticleCandidate, Evidence, FetchedArticle, Finding, RunSummary, Source, article_content_hash
from gaohe.storage import SCHEMA_VERSION, Store


KEY = "AIza" + "CliSecretKey" * 3 + "abc"
ARTICLE_URL = "https://news.test/a?api_key=article-url-secret&page=2"
FEED_URL = "https://news.test/feed.xml?token=feed-url-secret"


def test_version_command_prints_package_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == "gaohe 0.2.0\n"


def test_package_version_matches_pyproject():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["version"] == gaohe.__version__ == "0.2.0"


def test_doctor_reports_safe_provider_neutral_readiness(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_PROVIDER=gemini\n"
        "LLM_MODEL=gemini-test\n"
        "LLM_API_KEY=super-secret\n"
        "WEB_SEARCH_PROVIDER=firecrawl\n"
        "FIRECRAWL_API_KEY=firecrawl-secret\n",
        encoding="utf-8",
    )

    assert main(["doctor", "--env-file", str(env_file)]) == 0
    output = capsys.readouterr().out

    assert "llm_provider=gemini" in output
    assert "llm_model=configured" in output
    assert "web_search_provider=firecrawl" in output
    assert "llm_api_key=present" in output
    assert "firecrawl_api_key=present" in output
    assert "missing_setup=none" in output
    assert "data_dir=" not in output
    assert "database_path=" not in output
    assert "poll_interval_minutes=" not in output
    assert "super-secret" not in output
    assert "firecrawl-secret" not in output
    assert "GOOGLE_API_KEY" not in output


def test_doctor_lists_missing_required_setup_without_secret_names(tmp_path, capsys):
    assert main(["doctor", "--env-file", str(tmp_path / ".env")]) == 0
    output = capsys.readouterr().out

    assert "missing_setup=LLM_PROVIDER,LLM_MODEL,LLM_API_KEY" in output
    assert "GOOGLE_API_KEY" not in output


# --- status, finding, topic and serve ------------------------------------------------------------------

def _env(tmp_path, extra: str = "") -> str:
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATA_DIR={tmp_path / 'data'}\nLLM_API_KEY={KEY}\n{extra}", encoding="utf-8")
    return str(env_file)


def _open_store(env_file: str) -> Store:
    store = Store(load_settings(Path(env_file), environ={}).database_path)
    store.initialize()
    return store


def _save_article(store: Store, source_name: str, feed_url: str, url: str, title: str, text: str) -> int:
    source_id = store.add_source(Source(None, source_name, feed_url))
    candidate = ArticleCandidate(source_id, url, title, None, "2026-09-20T00:00:00Z", {})
    revision_id, _ = store.save_fetched_article(
        FetchedArticle(candidate, text, "2026-09-20T00:00:00Z", article_content_hash(title, text))
    )
    return revision_id


def _seeded_store(env_file: str) -> Store:
    store = _open_store(env_file)
    revision_id = _save_article(store, "範例日報", FEED_URL, ARTICLE_URL, "市府公布預算\n標題", "市府表示預算增加三成，議員質疑數字。")
    visible = Finding(None, revision_id, None, "factual_contradiction", "預算數字與公報不符", 5, 11, "resolved", "retrieved", True)
    hidden = Finding(
        None, revision_id, None, "unsupported_inference", "推論待補查 token=summary-secret", 0, 4, "pending", "pending", False,
    )
    evidence = Evidence(
        None, None, "https://record.test/budget", "公報", "公報寫增加一成", "contradicts", "retrieved", "direct",
        "2026-09-20T01:00:00Z", rationale="公報寫增加一成",
    )
    store.save_analysis(revision_id, (), (visible, hidden), ((evidence,), ()), completed_at="2026-09-20T02:00:00Z")
    store.record_source_check(1, "2026-09-20T00:00:00Z", "failed", 3, f"HTTP 500 for {FEED_URL}\nretry later")
    store.record_run(RunSummary("2026-09-20T00:00:00Z", "2026-09-20T00:01:00Z", 1, 3, 1, 1))
    return store


def _assert_clean(output: str) -> None:
    for secret in (KEY, "article-url-secret", "feed-url-secret", "summary-secret"):
        assert secret not in output


def test_status_prints_schema_counts_last_run_and_redacted_source_health(tmp_path, capsys):
    env_file = _env(tmp_path, "DAILY_LLM_CALL_LIMIT=25\n")
    _seeded_store(env_file)

    assert main(["status", "--env-file", env_file]) == 0
    lines = capsys.readouterr().out.splitlines()

    assert lines[0] == f"schema_version={SCHEMA_VERSION}"
    assert lines[1] == "analysis pending=0 running=0 completed=1 failed=0 skipped=0 unanalyzed=0"
    assert lines[2] == "llm_calls_today=0 daily_llm_call_limit=25"
    assert lines[3] == (
        "last_run started_at=2026-09-20T00:00:00Z finished_at=2026-09-20T00:01:00Z sources_checked=1 "
        "candidates_seen=3 revisions_created=1 failures=1"
    )
    assert lines[4].startswith("source id=1 enabled=yes status=failed checked_at=2026-09-20T00:00:00Z candidates_seen=3 ")
    assert "feed_url=https://news.test/feed.xml?token=%2A%2A%2A" in lines[4]
    assert "error=HTTP 500 for https://news.test/feed.xml?token=[redacted] retry later" in lines[4]
    assert lines[4].endswith("name=範例日報")
    assert len(lines) == 5
    _assert_clean("\n".join(lines))


def test_status_on_a_fresh_install_reports_no_run(tmp_path, capsys):
    assert main(["status", "--env-file", _env(tmp_path)]) == 0
    output = capsys.readouterr().out

    assert "last_run=none" in output
    assert "unanalyzed=0" in output and "source id=" not in output


def test_status_reports_invalid_configuration_without_details(tmp_path, capsys):
    assert main(["status", "--env-file", _env(tmp_path, "DAILY_LLM_CALL_LIMIT=0\n")]) == 2
    captured = capsys.readouterr()
    assert captured.err == "error: status unavailable\n" and captured.out == ""


def test_finding_list_shows_visible_findings_with_redacted_urls(tmp_path, capsys):
    env_file = _env(tmp_path)
    _seeded_store(env_file)

    assert main(["finding", "list", "--env-file", env_file]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "id=1 type=factual_contradiction evidence_status=retrieved review_status=unreviewed visible=yes "
        "title=市府公布預算 標題 url=https://news.test/a?api_key=%2A%2A%2A&page=2 summary=預算數字與公報不符"
    ]


def test_finding_list_all_includes_pending_findings_and_respects_limit(tmp_path, capsys):
    env_file = _env(tmp_path)
    _seeded_store(env_file)

    assert main(["finding", "list", "--all", "--env-file", env_file]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0].startswith("id=2 type=unsupported_inference evidence_status=pending")
    assert "visible=no" in output and "summary=推論待補查 token=[redacted]" in output
    _assert_clean(output)

    assert main(["finding", "list", "--all", "--limit", "1", "--env-file", env_file]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_finding_list_says_when_nothing_is_visible(tmp_path, capsys):
    env_file = _env(tmp_path)

    assert main(["finding", "list", "--env-file", env_file]) == 0
    assert capsys.readouterr().out == "no visible findings (use --all to include pending ones)\n"
    assert main(["finding", "list", "--all", "--env-file", env_file]) == 0
    assert capsys.readouterr().out == "no findings\n"


@pytest.mark.parametrize("limit", ["0", "-3", "many"])
def test_finding_and_topic_list_reject_invalid_limits(tmp_path, capsys, limit):
    env_file = _env(tmp_path)

    for command in ("finding", "topic"):
        assert main([command, "list", "--limit", limit, "--env-file", env_file]) == 2
        assert capsys.readouterr().err == "error: --limit must be a positive integer\n"


def test_finding_review_records_decision_and_redacted_note(tmp_path, capsys):
    env_file = _env(tmp_path)
    store = _seeded_store(env_file)

    assert main([
        "finding", "review", "--id", "1", "--status", "confirmed", "--note", "已核對 token=note-secret",
        "--env-file", env_file,
    ]) == 0
    assert capsys.readouterr().out == "reviewed finding id=1 status=confirmed\n"
    with sqlite3.connect(store.path) as connection:
        status, note, reviewed_at = connection.execute(
            "SELECT review_status, review_note, reviewed_at FROM findings WHERE id = 1"
        ).fetchone()
    assert (status, note) == ("confirmed", "已核對 token=[redacted]")
    assert reviewed_at.endswith("Z")

    assert main(["finding", "review", "--id", "1", "--status", "dismissed", "--env-file", env_file]) == 0
    assert store.list_findings()[0]["review_status"] == "dismissed"


def test_finding_review_of_unknown_id_exits_two(tmp_path, capsys):
    env_file = _env(tmp_path)
    _seeded_store(env_file)

    assert main(["finding", "review", "--id", "99", "--status", "confirmed", "--env-file", env_file]) == 2
    captured = capsys.readouterr()
    assert captured.err == "error: finding id=99 was not found\n" and captured.out == ""


def test_finding_review_accepts_only_human_decisions(tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["finding", "review", "--id", "1", "--status", "unreviewed", "--env-file", _env(tmp_path)])
    assert error.value.code == 2


def test_topic_list_and_review(tmp_path, capsys):
    env_file = _env(tmp_path)
    store = _open_store(env_file)
    first = _save_article(store, "甲報", "https://alpha.test/feed", "https://alpha.test/a", "甲報報導", "甲報內文。")
    second = _save_article(
        store, "乙報", "https://bravo.test/feed", "https://bravo.test/b?session=topic-secret", "乙報報導", "乙報內文。",
    )
    store.assign_topic(first, second, "行政院 開放外籍旅客入境", "possible")

    assert main(["topic", "list", "--env-file", env_file]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "id=1 status=possible confidence=possible articles=2 label=行政院 開放外籍旅客入境",
        "  revision_id=1 source=甲報 title=甲報報導 url=https://alpha.test/a",
        "  revision_id=2 source=乙報 title=乙報報導 url=https://bravo.test/b?session=%2A%2A%2A",
    ]

    assert main(["topic", "review", "--id", "1", "--status", "active", "--env-file", env_file]) == 0
    assert capsys.readouterr().out == "reviewed topic id=1 status=active\n"
    assert store.topic_status(1) == "active"

    assert main(["topic", "review", "--id", "1", "--status", "dismissed", "--env-file", env_file]) == 0
    assert store.topic_status(1) == "dismissed"
    capsys.readouterr()
    assert main(["topic", "list", "--env-file", env_file]) == 0
    assert capsys.readouterr().out == "no topics\n"


def test_topic_review_of_unknown_id_exits_two(tmp_path, capsys):
    assert main(["topic", "review", "--id", "7", "--status", "active", "--env-file", _env(tmp_path)]) == 2
    assert capsys.readouterr().err == "error: topic id=7 was not found\n"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "example.test", "127.0.0.2", ""])
def test_serve_refuses_hosts_other_than_this_computer(tmp_path, capsys, monkeypatch, host):
    monkeypatch.setattr(cli, "serve", lambda **kwargs: pytest.fail("the server must not start"))

    assert main(["serve", "--host", host, "--env-file", _env(tmp_path)]) == 2
    assert "error: --host must be 127.0.0.1, localhost or ::1" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_serve_accepts_loopback_hosts(tmp_path, monkeypatch, host):
    calls = []
    monkeypatch.setattr(cli, "serve", lambda **kwargs: calls.append(kwargs))
    env_file = _env(tmp_path)

    assert main(["serve", "--host", host, "--port", "8123", "--env-file", env_file]) == 0
    assert calls == [{"host": host, "port": 8123, "env_file": Path(env_file)}]


def test_serve_reports_a_busy_port_without_a_traceback(tmp_path, capsys, monkeypatch):
    def busy(**kwargs):
        raise OSError("[Errno 98] Address already in use")

    monkeypatch.setattr(cli, "serve", busy)

    assert main(["serve", "--env-file", _env(tmp_path)]) == 2
    assert capsys.readouterr().err == "error: serve unavailable\n"
