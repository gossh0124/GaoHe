from gaohe.cli import main


def test_version_command_prints_package_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == "gaohe 0.1.0\n"


def test_doctor_reports_safe_settings_without_api_key(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_MODEL=gemini-test\nGOOGLE_API_KEY=super-secret\n",
        encoding="utf-8",
    )

    assert main(["doctor", "--env-file", str(env_file)]) == 0
    output = capsys.readouterr().out

    assert "llm_provider=gemini" in output
    assert "gemini_model=gemini-test" in output
    assert "google_api_key=present" in output
    assert "super-secret" not in output
