import pytest

import gaohe.pipeline as pipeline
from gaohe import __version__, cli


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"DATA_DIR={tmp_path / 'data'}\nLLM_PROVIDER=gemini\nLLM_MODEL=m\nLLM_API_KEY=AIza-cli-secret\n", encoding="utf-8")
    return path


def test_version_and_doctor_never_print_the_key(env_file, capsys):
    assert cli.main(["--version"]) == 0
    assert cli.main(["doctor", "--env-file", str(env_file)]) == 0
    output = capsys.readouterr().out
    assert f"gaohe {__version__}" in output and "llm_api_key=present" in output and "missing_setup=none" in output
    assert "AIza-cli-secret" not in output


def test_source_commands_redact_urls_and_validate_input(env_file, capsys):
    url = "https://wire.example/rss?apiKey=WIRE-SECRET&lang=zh"
    assert cli.main(["source", "add", "--name", "通訊社", "--feed-url", url, "--env-file", str(env_file)]) == 0
    assert cli.main(["source", "list", "--env-file", str(env_file)]) == 0
    assert cli.main(["source", "disable", "--id", "1", "--env-file", str(env_file)]) == 0
    assert cli.main(["source", "enable", "--id", "99", "--env-file", str(env_file)]) == 2
    assert cli.main(["source", "add", "--name", "x", "--feed-url", "file:///etc", "--env-file", str(env_file)]) == 2
    output = capsys.readouterr()
    assert "WIRE-SECRET" not in output.out and "lang=zh" in output.out and "disabled source id=1" in output.out
    assert "must be an HTTP(S) URL" in output.err


def test_serve_refuses_non_loopback_hosts_and_bad_ports(env_file, capsys):
    assert cli.main(["serve", "--host", "0.0.0.0", "--env-file", str(env_file)]) == 2
    assert cli.main(["serve", "--port", "70000", "--env-file", str(env_file)]) == 2
    assert "this computer only" in capsys.readouterr().err


def test_analyze_needs_setup_and_exits_3_when_the_run_stops(env_file, tmp_path, monkeypatch, capsys):
    assert cli.main(["analyze", "--pending", "--env-file", str(tmp_path / "missing.env")]) == 2
    monkeypatch.setattr(cli, "_providers", lambda settings: (None, None, None, None))
    monkeypatch.setattr(pipeline, "run_pending_analysis", lambda *a, **k: {"analyzed": 0, "stopped": 1, "stop_code": "auth"})
    assert cli.main(["analyze", "--pending", "--env-file", str(env_file)]) == 3
    assert "金鑰" in capsys.readouterr().err


def test_check_prints_the_plain_message(env_file, monkeypatch, capsys):
    import gaohe.checks as checks
    from gaohe.domain import CheckOutcome

    monkeypatch.setattr(cli, "_providers", lambda settings: (None, None, None, None))
    monkeypatch.setattr(checks, "check_article_url", lambda *a, **k: CheckOutcome("completed", "查核完成。", 1))
    assert cli.main(["check", "https://news.example/a", "--env-file", str(env_file)]) == 0
    assert capsys.readouterr().out.strip() == "查核完成。"
