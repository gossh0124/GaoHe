from pathlib import Path

import pytest

from gaohe.config import Settings, load_settings


def test_defaults_need_setup_and_use_gemini_search():
    settings = load_settings(Path("missing.env"), environ={"LOCALAPPDATA": "C:/Users/me/AppData/Local"})
    assert settings.web_search_provider == "gemini"
    assert settings.poll_interval_minutes == 60
    assert settings.database_path == Path("C:/Users/me/AppData/Local") / "GaoHe" / "gaohe.db"
    assert settings.validate() == ["LLM_PROVIDER must be gemini", "LLM_MODEL is required", "LLM_API_KEY is required"]


def test_env_file_values_are_read_and_environment_wins(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nLLM_PROVIDER=gemini\nLLM_MODEL='gemini-2.5-flash'\nLLM_API_KEY=file-key\nDATA_DIR=data\n", encoding="utf-8")
    settings = load_settings(env, environ={"LLM_API_KEY": "env-key"})
    assert (settings.llm_provider, settings.llm_model, settings.llm_api_key) == ("gemini", "gemini-2.5-flash", "env-key")
    assert settings.data_dir == Path("data") and settings.validate() == []
    assert "env-key" not in repr(settings)


def test_legacy_names_are_still_read(tmp_path):
    settings = load_settings(tmp_path / "none.env", environ={"GEMINI_MODEL": "m", "GOOGLE_API_KEY": "k"})
    assert (settings.llm_model, settings.llm_api_key) == ("m", "k")


@pytest.mark.parametrize("value", ["0", "-5", "hourly", "1.5"])
def test_invalid_poll_interval_raises_without_echoing_the_value(tmp_path, value):
    with pytest.raises(ValueError) as error:
        load_settings(tmp_path / "none.env", environ={"POLL_INTERVAL_MINUTES": value})
    assert value not in str(error.value) or value == "0"


def test_unknown_search_provider_is_reported():
    assert "WEB_SEARCH_PROVIDER must be gemini or none" in Settings(web_search_provider="bing").validate()


def test_env_example_lists_every_setting():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    names = {line.split("=", 1)[0] for line in example.read_text(encoding="utf-8").splitlines() if "=" in line}
    assert names == {"LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "WEB_SEARCH_PROVIDER", "DATA_DIR", "POLL_INTERVAL_MINUTES"}
