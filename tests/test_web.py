from pathlib import Path

from gaohe.config import Settings
from gaohe.web import render_status_page


def test_status_page_contains_safe_runtime_state_without_key():
    settings = Settings(
        llm_provider="gemini",
        gemini_model="gemini-2.5-flash-lite",
        search_provider="none",
        data_dir=Path("data"),
        google_api_key="do-not-render",
    )

    page = render_status_page(settings)

    assert "GaoHe local runtime" in page
    assert "gemini-2.5-flash-lite" in page
    assert "do-not-render" not in page
