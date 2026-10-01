import io
import json
from urllib.error import HTTPError, URLError

import pytest

from conftest import ARTICLE_TEXT, FakeGemini, FakeTransport, gemini_text, html_response, revision
from gaohe.config import Settings
from gaohe.domain import Evidence, FindingCandidate
from gaohe.errors import ProviderError
from gaohe.providers import (
    MAX_RETRIES,
    DirectPageFetcher,
    GeminiAnalysisProvider,
    GeminiClient,
    GeminiEvidenceAssessor,
    GeminiSearchProvider,
    NullSearchProvider,
    build_search_provider,
    contains_quote,
    locate_quote,
    response_text,
)
from gaohe.sources import HttpResponse

SETTINGS = Settings(llm_provider="gemini", llm_model="gemini-2.5-flash", llm_api_key="AIza-test-key")


def http_error(code, body=b"{}", headers=None):
    return HTTPError("https://x.test", code, "error", headers or {}, io.BytesIO(body))


# --- quote anchoring --------------------------------------------------------------------------

def test_locate_quote_exact_whitespace_insensitive_and_occurrence():
    text = "近 500 位民眾到場。主辦單位說近500位。"
    start = text.index("主辦單位說")
    assert locate_quote(text, "主辦單位說") == (start, start + 5)
    assert locate_quote("近 500 位民眾", "近500位") == (0, 7)  # span covers the original spacing
    assert locate_quote("甲乙甲乙", "甲乙") is None  # ambiguous without occurrence
    assert locate_quote("甲乙甲乙", "甲乙", 2) == (2, 4)
    assert locate_quote("甲乙甲乙", "甲乙", 3) is None
    assert locate_quote("abc", "  ") is None and locate_quote("abc", "zzz") is None


def test_contains_quote_ignores_whitespace_but_needs_content():
    assert contains_quote("官員 表示 增加三成", "官員表示")
    assert not contains_quote("官員表示", "") and not contains_quote("官員表示", "減少")


# --- client: retry, classification, no leaks --------------------------------------------------

def test_client_retries_rate_limit_using_retry_info_then_succeeds():
    sleeps, attempts = [], []

    def request(model, payload, key):
        attempts.append(1)
        if len(attempts) == 1:
            raise http_error(429, b'{"error":{"details":[{"retryDelay":"7s"}]}}')
        return {"ok": True}

    client = GeminiClient(SETTINGS, request, sleep=sleeps.append)
    assert client.generate({}) == {"ok": True}
    assert sleeps == [7.0] and client.requests_sent == 2


@pytest.mark.parametrize("error, code", [
    (http_error(400, b'{"error":{"details":[{"reason":"API_KEY_INVALID"}]}}'), "auth"),
    (http_error(403), "auth"),
    (http_error(404), "model_not_found"),
    (http_error(400), "invalid_response"),
    (http_error(503), "unavailable"),
    (URLError(TimeoutError()), "timeout"),
    (URLError("dns"), "network"),
    (ConnectionResetError(), "network"),
])
def test_client_classifies_failures_and_never_chains_the_original(error, code):
    sleeps = []
    client = GeminiClient(SETTINGS, FakeGemini(error=error), sleep=sleeps.append)
    with pytest.raises(ProviderError) as raised:
        client.generate({})
    assert raised.value.code == code
    assert raised.value.__context__ is None and raised.value.__cause__ is None
    retried = code in ("unavailable", "timeout", "network")
    assert len(sleeps) == (MAX_RETRIES if retried else 0)


def test_client_requires_model_and_key():
    with pytest.raises(ProviderError) as raised:
        GeminiClient(Settings(llm_provider="gemini"), FakeGemini()).generate({})
    assert raised.value.code == "config"


def test_real_post_sends_key_only_in_header():
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()), body=request.data, timeout=timeout)
        return Response(json.dumps({"candidates": []}).encode())

    GeminiClient(SETTINGS, urlopen_request=fake_urlopen).generate({"contents": []})
    assert "AIza-test-key" not in seen["url"] and b"AIza-test-key" not in seen["body"]
    assert seen["headers"]["X-goog-api-key"] == "AIza-test-key"
    assert seen["url"].endswith("/models/gemini-2.5-flash:generateContent")


@pytest.mark.parametrize("value, code", [
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "blocked"),
    ({"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]}, "blocked"),
    ({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}, "invalid_response"),
    ({}, "invalid_response"),
])
def test_response_text_reports_blocked_and_empty_answers(value, code):
    with pytest.raises(ProviderError) as raised:
        response_text(value)
    assert raised.value.code == code


# --- analysis -------------------------------------------------------------------------------

def test_analysis_anchors_quotes_drops_unusable_claims_and_gates_candidates():
    gemini = FakeGemini(analysis={
        "claims": [
            {"quote": "開放外籍旅客入境觀光", "kind": "checkable", "materiality": "material"},
            {"quote": "政策將使觀光收入增加三成", "kind": "inference", "materiality": "material"},
            {"quote": "不存在的句子", "kind": "checkable", "materiality": "material"},
            {"quote": "行政院今天宣布", "kind": "descriptive", "materiality": "ordinary"},
            {"quote": "開放外籍旅客入境觀光", "kind": "checkable", "materiality": "material"},  # duplicate span
        ],
        "candidates": [
            {"claim_index": 0, "finding_type": "factual_contradiction", "summary": "確認開放日期", "query": "開放 外籍旅客 入境"},
            {"claim_index": 1, "finding_type": "unsupported_inference", "summary": "收入增幅缺乏依據"},
            {"claim_index": 2, "finding_type": "factual_contradiction", "summary": "dropped claim"},
            {"claim_index": 3, "finding_type": "factual_contradiction", "summary": "descriptive claim"},
            {"claim_index": 0, "finding_type": "material_cross_media_difference", "summary": "removed type"},
        ],
    })
    result = GeminiAnalysisProvider(SETTINGS, gemini).analyze(revision())
    assert [ARTICLE_TEXT[c.start:c.end] for c in result.claims] == ["開放外籍旅客入境觀光", "政策將使觀光收入增加三成", "行政院今天宣布"]
    assert result.rejected_claims == 2
    assert [(c.finding_type, c.query) for c in result.candidates] == [
        ("factual_contradiction", "開放 外籍旅客 入境"), ("unsupported_inference", None),
    ]
    assert gemini.calls[0][0] == "analysis"


def test_analysis_rejects_a_malformed_top_level_response():
    gemini = lambda model, payload, key: gemini_text({"claims": "nope"})  # noqa: E731
    with pytest.raises(ProviderError):
        GeminiAnalysisProvider(SETTINGS, gemini).analyze(revision())


# --- assessment and search --------------------------------------------------------------------

def test_assessor_sends_context_around_the_anchored_span():
    text = "背景。" * 400 + "官員表示增加三成。" + "後記。" * 400
    start = text.index("官員")
    captured = {}

    def assessment(payload):
        captured.update(json.loads(payload["contents"][0]["parts"][0]["text"]))
        return {"evidence_quote": "增加一成", "rationale": "官方數字不同", "relation": "contradicts"}

    candidate = FindingCandidate("factual_contradiction", "收入", start, start + 8, None, 1)
    evidence = Evidence("https://gov.example/a", "官方", "預估增加一成", "context", "retrieved", None)
    result = GeminiEvidenceAssessor(SETTINGS, FakeGemini(assessment=assessment)).assess(
        candidate, "官員表示增加三成", revision(text), evidence
    )
    assert result.relation == "contradicts" and "官員表示增加三成" in captured["article"]["context"]


def test_search_returns_grounding_sources_as_leads_and_honours_limit():
    gemini = FakeGemini(search_uris=["https://a.example/1", "https://a.example/1", "ftp://x", "https://b.example/2", "https://c.example/3"])
    hits = GeminiSearchProvider(SETTINGS, gemini).search("開放 入境", limit=2)
    assert [hit.url for hit in hits] == ["https://a.example/1", "https://b.example/2"]
    assert gemini.calls[0][3]["tools"] == [{"google_search": {}}]


def test_build_search_provider_follows_settings():
    assert isinstance(build_search_provider(SETTINGS), GeminiSearchProvider)
    assert isinstance(build_search_provider(Settings(web_search_provider="none")), NullSearchProvider)


# --- page fetching ----------------------------------------------------------------------------

def test_direct_fetcher_extracts_text_and_follows_the_final_url():
    response = html_response("https://final.example/story", "標題", "第一段", "第二段")
    page = DirectPageFetcher(FakeTransport({"https://lead.example/x": response})).fetch("https://lead.example/x")
    assert (page.status, page.url, page.title, page.text) == ("retrieved", "https://final.example/story", "標題", "第一段\n第二段")


@pytest.mark.parametrize("page, status", [
    (HttpResponse(500, "https://x.test/", {}, b""), "http_error"),
    (HttpResponse(200, "https://x.test/", {"Content-Type": "text/html"}, b"<html></html>"), "parse_error"),
    (TimeoutError(), "timeout"),
    (OSError("refused"), "retrieval_failed"),
])
def test_direct_fetcher_failures_are_statuses_not_exceptions(page, status):
    assert DirectPageFetcher(FakeTransport({"https://x.test/": page})).fetch("https://x.test/").status == status
