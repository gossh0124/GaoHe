from pathlib import Path
from html.parser import HTMLParser

from gaohe.config import Settings
from gaohe.domain import Finding
from gaohe.web import render_article, render_setup_page, render_status_page


class _ArticleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.detail_depth = 0
        self.tags = []

    def handle_starttag(self, tag, attrs):
        is_detail = tag == "span" and ("class", "annotation-detail") in attrs
        self.tags.append((tag, is_detail))
        self.detail_depth += is_detail

    def handle_endtag(self, tag):
        if self.tags and self.tags[-1][0] == tag:
            _, is_detail = self.tags.pop()
            self.detail_depth -= is_detail

    def handle_data(self, data):
        if not self.detail_depth:
            self.parts.append(data)


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
    parser = _ArticleText()
    parser.feed(article)
    assert "".join(parser.parts) == "abcdef"
    assert "Factual contradiction: Pending check" in article
    assert "Unsupported inference: Evidence retrieved" in article
    assert "badge-pending" in article and "badge-retrieved" in article


def test_monitoring_snapshot_escapes_source_errors_and_never_echoes_key():
    page = render_status_page({
        "inbox": ({"title": "<article>", "source": "<source>", "text": "safe"},),
        "findings": ({"finding_type": "factual_contradiction", "summary": "<summary>", "evidence_status": "retrieval_failed"},),
        "comparisons": ({"label": "<comparison>", "confidence": "high"},),
        "sources": ({"name": "<source>", "error": '{"authorization":"never-show"}', "checked_at": "now", "status": "failed"},),
        "runtime": {"Provider diagnostic": '{"api_key":"do-not-render"}', "LLM API key": "present"},
    })
    assert "&lt;article&gt;" in page and "&lt;summary&gt;" in page and "&lt;comparison&gt;" in page
    assert "do-not-render" not in page and "never-show" not in page
    assert "sensitive details hidden" in page


def test_evidence_links_are_safe_escaped_and_available_on_articles_and_findings():
    page = render_status_page({
        "inbox": ({"text": "safe", "evidence": {"url": "https://record.example/evidence?a=1&b=2", "title": "<record>", "provider": "<provider>", "retrieved_at": "now"}},),
        "findings": ({"summary": "safe", "evidence": {"url": "https://record.example/finding", "title": "Finding evidence"}},),
        "sources": (), "comparisons": (), "runtime": {},
    })
    assert "https://record.example/evidence?a=1&amp;b=2" in page
    assert "&lt;record&gt;" in page and "&lt;provider&gt;" in page
    assert "https://record.example/finding" in page and "Finding evidence" in page
    rejected = render_status_page({
        "inbox": ({"text": "safe", "evidence": ({"url": "javascript:alert(1)", "title": "never"}, {"url": "https://user:secret@record.example/private", "title": "also never"})},),
        "findings": (), "sources": (), "comparisons": (), "runtime": {},
    })
    assert "javascript:" not in rejected and "user:secret" not in rejected and "also never" not in rejected


def test_evidence_redacts_url_secrets_and_hides_sensitive_metadata():
    page = render_status_page({
        "inbox": ({"text": "safe", "evidence": {
            "url": "https://record.example/evidence?api_key=never-show#access_token=also-never-show",
            "title": '{"api_key":"metadata-secret"}',
            "provider": "Bearer provider-secret",
            "retrieved_at": '"authorization":"timestamp-secret"',
        }},),
        "findings": (), "sources": (), "comparisons": (), "runtime": {},
    })
    for secret in ("never-show", "also-never-show", "metadata-secret", "provider-secret", "timestamp-secret"):
        assert secret not in page
    assert "record.example/evidence" not in page
    ordinary = render_status_page({
        "inbox": ({"text": "safe", "evidence": {
            "url": "https://record.example/evidence#section-1",
            "title": "Official record", "provider": "Public archive", "retrieved_at": "2026-09-21T00:00:00Z",
        }},),
        "findings": (), "sources": (), "comparisons": (), "runtime": {},
    })
    for value in ("Official record", "Public archive", "2026-09-21T00:00:00Z"):
        assert value in ordinary
    assert "#section-1" in ordinary


def test_evidence_rejects_percent_encoded_sensitive_fragment():
    url = "https://record.example/evidence#api%5Fkey%3Dfragment-secret"
    page = render_status_page({
        "inbox": ({"text": "safe", "evidence": {"url": url, "title": "never"}},),
        "findings": (), "sources": (), "comparisons": (), "runtime": {},
    })
    assert "fragment-secret" not in page and url not in page and "never" not in page


def test_setup_page_masks_state_and_settings_remain_compatible():
    setup = render_setup_page({"configured": True, "has_llm_key": True, "source_count": 1, "api_key": "secret"})
    assert "secret" not in setup and "AI key: present" in setup
    status = render_status_page(Settings(llm_api_key="private", data_dir=Path("data")))
    assert "private" not in status and "GaoHe local monitoring" in status
