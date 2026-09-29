from pathlib import Path

from gaohe.config import Settings
from gaohe.web import render_status_page


def test_status_page_contains_safe_runtime_state_without_key():
    settings = Settings(
        llm_provider="gemini",
        llm_model="gemini-2.5-flash-lite",
        web_search_provider="none",
        data_dir=Path("data"),
        llm_api_key="do-not-render",
    )

    page = render_status_page(settings)

    assert "本機設定" in page and "<code>LLM_MODEL</code>" in page
    assert "gemini-2.5-flash-lite" in page
    assert "已設定" in page
    assert "do-not-render" not in page
