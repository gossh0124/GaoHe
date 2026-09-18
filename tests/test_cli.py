from gaohe.cli import main


def test_version_command_prints_package_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == "gaohe 0.1.0\n"


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
    assert "super-secret" not in output
    assert "firecrawl-secret" not in output
    assert "GOOGLE_API_KEY" not in output


def test_doctor_lists_missing_required_setup_without_secret_names(tmp_path, capsys):
    assert main(["doctor", "--env-file", str(tmp_path / ".env")]) == 0
    output = capsys.readouterr().out

    assert "missing_setup=LLM_PROVIDER,LLM_MODEL,LLM_API_KEY" in output
    assert "GOOGLE_API_KEY" not in output
