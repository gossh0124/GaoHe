from pathlib import Path

import pytest

from gaohe.config import load_settings


def test_defaults_are_provider_neutral_and_do_not_create_storage(tmp_path: Path):
    settings = load_settings(tmp_path / ".env", {"LOCALAPPDATA": str(tmp_path / "profile")})

    assert settings.llm_provider == ""
    assert settings.llm_model == ""
    assert settings.llm_api_key == ""
    assert settings.web_search_provider == "none"
    assert settings.firecrawl_api_key == ""
    assert settings.poll_interval_minutes == 60
    assert settings.data_dir == tmp_path / "profile" / "GaoHe"
    assert settings.database_path == settings.data_dir / "gaohe.db"
    assert not settings.data_dir.exists()
    assert settings.validate() == [
        "LLM_PROVIDER is required",
        "LLM_MODEL is required",
        "LLM_API_KEY is required",
    ]


def test_explicit_environment_wins_and_secrets_are_not_repr(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_MODEL=file-model\nLLM_API_KEY=file-secret\n",
        encoding="utf-8",
    )

    settings = load_settings(
        env_file,
        {
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "env-model",
            "LLM_API_KEY": "env-secret",
            "WEB_SEARCH_PROVIDER": "firecrawl",
            "FIRECRAWL_API_KEY": "firecrawl-secret",
            "DATA_DIR": str(tmp_path / "runtime"),
            "POLL_INTERVAL_MINUTES": "15",
        },
    )

    assert settings.llm_provider == "openai"
    assert settings.llm_model == "env-model"
    assert settings.llm_api_key == "env-secret"
    assert settings.web_search_provider == "firecrawl"
    assert settings.firecrawl_api_key == "firecrawl-secret"
    assert settings.data_dir == tmp_path / "runtime"
    assert settings.poll_interval_minutes == 15
    assert settings.has_llm_key
    assert settings.has_firecrawl_key
    assert settings.validate() == []
    assert "env-secret" not in repr(settings)
    assert "firecrawl-secret" not in repr(settings)


def test_legacy_local_environment_migrates_when_generic_names_are_absent(tmp_path: Path):
    settings = load_settings(
        tmp_path / ".env",
        {"GEMINI_MODEL": "gemini-test", "GOOGLE_API_KEY": "legacy-secret"},
    )

    assert settings.llm_model == "gemini-test"
    assert settings.llm_api_key == "legacy-secret"


@pytest.mark.parametrize("value", ["0", "-1", "often"])
def test_invalid_poll_interval_raises_clear_error(tmp_path: Path, value: str):
    with pytest.raises(ValueError, match="POLL_INTERVAL_MINUTES must be a positive integer"):
        load_settings(tmp_path / ".env", {"POLL_INTERVAL_MINUTES": value})
