from pathlib import Path

from gaohe.config import load_settings


def test_free_test_defaults_do_not_require_a_key(tmp_path: Path):
    settings = load_settings(tmp_path / ".env", {})

    assert settings.llm_provider == "gemini"
    assert settings.gemini_model == "gemini-2.5-flash-lite"
    assert settings.search_provider == "none"
    assert settings.google_api_key is None


def test_explicit_environment_wins_and_key_is_not_repr(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_MODEL=file-model\nGOOGLE_API_KEY=file-secret\n",
        encoding="utf-8",
    )

    settings = load_settings(
        env_file,
        {"GEMINI_MODEL": "env-model", "GOOGLE_API_KEY": "env-secret"},
    )

    assert settings.gemini_model == "env-model"
    assert settings.google_api_key == "env-secret"
    assert "env-secret" not in repr(settings)
