from pathlib import Path

from gaohe.config import Settings
from gaohe.domain import Finding
from gaohe.web import render_article, render_setup_page, render_status_page


def finding(kind="factual_contradiction", start=0, end=4, status="retrieved", summary="detail"):
    return Finding(None, 1, None, kind, summary, start, end, "open", status, True)


def test_article_escapes_plain_text_and_clips_invalid_spans():
    assert render_article("<plain & text>", ()) == "&lt;plain &amp; text&gt;"
    page = render_article("abcd", (finding(start=-4, end=99, summary="<unsafe>"),))
    assert "abcd" in page and "&lt;unsafe&gt;" in page


def test_article_uses_all_semantic_colors_labels_and_evidence_badges():
    article = render_article("abcdefgh", (
        finding("factual_contradiction", 0, 2, "pending"),
        finding("material_cross_media_difference", 2, 4, "retrieval_failed"),
        finding("unsupported_inference", 4, 8, "insufficient_scope"),
        finding("factual_contradiction", 6, 8, "retrieved"),
    ))
    for value in ("mark-factual_contradiction", "mark-material_cross_media_difference", "mark-unsupported_inference", "Factual contradiction", "Material cross-media difference", "Unsupported inference", "Pending check", "Retrieval failed", "Insufficient scope", "Evidence retrieved"):
        assert value in article


def test_overlap_renders_each_original_character_once_with_all_details():
    article = render_article("abcdef", (finding(start=1, end=5, status="pending"), finding("unsupported_inference", 3, 6, "retrieved")))
    assert article.count("abcdef") == 0
    assert "bc" in article and "de" in article and "f" in article
    assert "Factual contradiction: Pending check" in article
    assert "Unsupported inference: Evidence retrieved" in article


def test_monitoring_snapshot_escapes_source_errors_and_never_echoes_key():
    page = render_status_page({
        "inbox": ({"title": "<article>", "source": "<source>", "text": "safe"},),
        "findings": ({"finding_type": "factual_contradiction", "summary": "<summary>", "evidence_status": "retrieval_failed"},),
        "comparisons": ({"label": "<comparison>", "confidence": "high"},),
        "sources": ({"name": "<source>", "error": "api_key=never-show", "checked_at": "now", "status": "failed"},),
        "runtime": {"secret": "do-not-render", "LLM API key": "present"},
    })
    assert "&lt;article&gt;" in page and "&lt;summary&gt;" in page and "&lt;comparison&gt;" in page
    assert "do-not-render" not in page and "api_key=never-show" not in page
    assert "sensitive details hidden" in page


def test_setup_page_masks_state_and_settings_remain_compatible():
    setup = render_setup_page({"configured": True, "has_llm_key": True, "source_count": 1, "api_key": "secret"})
    assert "secret" not in setup and "AI key: present" in setup
    status = render_status_page(Settings(llm_api_key="private", data_dir=Path("data")))
    assert "private" not in status and "GaoHe local monitoring" in status
