from dataclasses import dataclass

import pytest

from gaohe.config import Settings
from gaohe.domain import ArticleRevision, RetrievedPage, article_content_hash
from gaohe.sources import HttpResponse


def revision() -> ArticleRevision:
    text = "A checkable statement."
    return ArticleRevision(1, 1, "https://news.test/article", "Article", text, article_content_hash("Article", text), "2026-09-18T00:00:00Z")


@dataclass
class FakeTransport:
    response: HttpResponse | Exception
    calls: int = 0

    def fetch(self, _url: str, timeout_seconds: float = 20.0) -> HttpResponse:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_null_search_and_unsupported_selection_are_network_free():
    from gaohe.providers import NullSearchProvider, build_analysis_provider, build_search_provider

    assert NullSearchProvider().search("anything") == ()
    assert build_search_provider(Settings(llm_provider="gemini", llm_model="gemini", llm_api_key="key")).search("anything") == ()
    with pytest.raises(ValueError, match="Unsupported WEB_SEARCH_PROVIDER: example"):
        build_search_provider(Settings(web_search_provider="example"))
    with pytest.raises(ValueError, match="Unsupported LLM_PROVIDER: example"):
        build_analysis_provider(Settings(llm_provider="example"))


def test_gemini_adapter_parses_strict_valid_json_and_redacts_failures():
    from gaohe.providers import GeminiAnalysisProvider

    response = '{"claims":[{"text":"A checkable statement.","start":0,"end":22,"kind":"checkable","materiality":"ordinary"}],"candidates":[{"claim_id":null,"finding_type":"factual_contradiction","summary":"Needs checking","start":0,"end":22,"materiality":"material","query":"source query"}]}'
    provider = GeminiAnalysisProvider(Settings(llm_provider="gemini", llm_model="gemini-test", llm_api_key="super-secret"), request=lambda *_args: response)

    result = provider.analyze(revision(), ())

    assert result.revision_id == 1
    assert result.claims[0].text == "A checkable statement."
    assert result.candidates[0].query == "source query"
    bad = GeminiAnalysisProvider(Settings(llm_provider="gemini", llm_model="gemini", llm_api_key="super-secret"), request=lambda *_args: "not json")
    with pytest.raises(ValueError, match="Gemini returned invalid analysis response") as error:
        bad.analyze(revision(), ())
    assert "super-secret" not in str(error.value)


@pytest.mark.parametrize("response", ["{}", '{"claims":"bad","candidates":[]}'])
def test_gemini_adapter_rejects_invalid_shapes(response: str):
    from gaohe.providers import GeminiAnalysisProvider

    provider = GeminiAnalysisProvider(Settings(llm_provider="gemini", llm_model="gemini", llm_api_key="secret"), request=lambda *_args: response)
    with pytest.raises(ValueError, match="Gemini returned invalid analysis response"):
        provider.analyze(revision(), ())


def test_gemini_adapter_normalizes_provider_failure_without_secret_leakage():
    from gaohe.providers import GeminiAnalysisProvider

    def failed_request(*_args):
        raise RuntimeError("Authorization: Bearer super-secret")

    provider = GeminiAnalysisProvider(Settings(llm_provider="gemini", llm_model="gemini", llm_api_key="super-secret"), request=failed_request)
    with pytest.raises(ValueError, match="Gemini analysis request failed") as error:
        provider.analyze(revision(), ())
    assert "super-secret" not in str(error.value)


def test_gemini_post_keeps_key_out_of_url_headers_and_public_error():
    from gaohe.providers import GeminiAnalysisProvider

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            raise RuntimeError("https://example.test/?key=super-secret")

    captured = []

    def fake_urlopen(request, *, timeout):
        captured.append((request.full_url, dict(request.header_items()), timeout))
        return Response()

    provider = GeminiAnalysisProvider(
        Settings(llm_provider="gemini", llm_model="gemini-test", llm_api_key="super-secret"),
        urlopen_request=fake_urlopen,
    )
    with pytest.raises(ValueError, match="Gemini analysis request failed") as error:
        provider.analyze(revision(), ())

    url, headers, timeout = captured[0]
    assert "super-secret" not in url
    assert "?key=" not in url
    assert headers["X-goog-api-key"] == "super-secret"
    assert timeout == 20
    assert "super-secret" not in str(error.value)
    assert error.value.__cause__ is None


def test_gemini_adapter_bounds_candidate_query():
    from gaohe.providers import GeminiAnalysisProvider, MAX_QUERY_CHARS

    response = ('{"claims":[],"candidates":[{"claim_id":null,"finding_type":"factual_contradiction",'
                '"summary":"Needs checking","start":0,"end":1,"materiality":"material","query":"' + "q" * (MAX_QUERY_CHARS + 1) + '"}]}')
    provider = GeminiAnalysisProvider(Settings(llm_provider="gemini", llm_model="gemini", llm_api_key="secret"), request=lambda *_args: response)

    assert len(provider.analyze(revision(), ()).candidates[0].query) == MAX_QUERY_CHARS


def test_direct_page_fetcher_extracts_text_hash_and_uses_no_fallback_on_success():
    from gaohe.providers import DirectPageFetcher

    direct = FakeTransport(HttpResponse(200, "https://evidence.test/article", {"Content-Type": "text/html"}, b"<title>Evidence</title><main><p>Useful text</p></main>"))
    fallback = lambda _url: pytest.fail("fallback must not run")

    page = DirectPageFetcher(direct, fallback=fallback, firecrawl_api_key="key").fetch("https://evidence.test/article")

    assert (page.status, page.title, page.text, page.content_hash) == ("retrieved", "Evidence", "Useful text", article_content_hash("Evidence", "Useful text"))
    assert direct.calls == 1


@pytest.mark.parametrize(
    ("url", "response", "status"),
    [
        ("ftp://evidence.test/article", HttpResponse(200, "", {}, b""), "invalid_url"),
        ("https://evidence.test/article", HttpResponse(404, "https://evidence.test/article", {}, b""), "http_error"),
        ("https://evidence.test/article", TimeoutError("Bearer secret"), "timeout"),
        ("https://evidence.test/article", HttpResponse(200, "https://evidence.test/article", {"Content-Type": "text/html"}, b"x" * 1_000_001), "oversized"),
    ],
)
def test_direct_page_fetcher_normalizes_failures(url, response, status):
    from gaohe.providers import DirectPageFetcher

    page = DirectPageFetcher(FakeTransport(response)).fetch(url)

    assert page.status == status
    assert page.text == ""
    assert page.content_hash is None


def test_direct_fetcher_uses_injected_firecrawl_fallback_only_with_key():
    from gaohe.providers import DirectPageFetcher

    calls: list[str] = []

    def fallback(url: str):
        calls.append(url)
        return ("Fallback", "Fallback text")

    failed = FakeTransport(HttpResponse(503, "https://evidence.test/article", {}, b""))
    without_key = DirectPageFetcher(failed, fallback=fallback).fetch("https://evidence.test/article")
    assert without_key.status == "http_error"
    assert calls == []

    with_key = DirectPageFetcher(failed, fallback=fallback, firecrawl_api_key="secret").fetch("https://evidence.test/article")
    assert (with_key.status, with_key.title, with_key.text) == ("retrieved", "Fallback", "Fallback text")
    assert calls == ["https://evidence.test/article"]


def test_direct_fetcher_normalizes_fallback_page_and_recomputes_hash():
    from gaohe.providers import DirectPageFetcher, MAX_PAGE_TEXT_CHARS, MAX_PAGE_TITLE_CHARS

    fallback = lambda _url: RetrievedPage(
        "https://fallback.test/article",
        "T" * (MAX_PAGE_TITLE_CHARS + 1),
        "X" * (MAX_PAGE_TEXT_CHARS + 1),
        "2000-01-01T00:00:00Z",
        "retrieved",
        "forged-hash",
    )
    page = DirectPageFetcher(FakeTransport(HttpResponse(503, "https://evidence.test/article", {}, b"")), fallback=fallback, firecrawl_api_key="secret").fetch("https://evidence.test/article")

    assert (page.status, len(page.title), len(page.text)) == ("retrieved", MAX_PAGE_TITLE_CHARS, MAX_PAGE_TEXT_CHARS)
    assert page.content_hash == article_content_hash(page.title, page.text)
    assert page.content_hash != "forged-hash"


@pytest.mark.parametrize(
    "fallback",
    [
        lambda _url: RetrievedPage("ftp://fallback.test/article", "Title", "Text", "", "retrieved", None),
        lambda _url: ("Title", ""),
        lambda _url: ("", "body"),
        lambda _url: (" \t", "body"),
    ],
)
def test_direct_fetcher_rejects_invalid_or_incomplete_fallback_page(fallback):
    from gaohe.providers import DirectPageFetcher

    page = DirectPageFetcher(FakeTransport(HttpResponse(503, "https://evidence.test/article", {}, b"")), fallback=fallback, firecrawl_api_key="secret").fetch("https://evidence.test/article")

    assert (page.status, page.content_hash) == ("http_error", None)


@pytest.mark.parametrize("result", ["not a tuple", ("title",), ("title", None)])
def test_firecrawl_fetcher_maps_malformed_output_to_safe_status(result):
    from gaohe.providers import FirecrawlPageFetcher

    page = FirecrawlPageFetcher("secret", lambda *_args: result).fetch("https://evidence.test/article")

    assert (page.status, page.text, page.content_hash) == ("retrieval_failed", "", None)


def test_provider_matrix_runs_with_null_search_and_fake_analysis_without_network():
    from gaohe.providers import AnalysisResult, NullSearchProvider

    class FakeAnalysis:
        def analyze(self, item, related):
            assert item == revision()
            assert related == ()
            return AnalysisResult(item.id, (), ())

    assert NullSearchProvider().search("bounded query", limit=999) == ()
    assert FakeAnalysis().analyze(revision(), ()).revision_id == 1


@pytest.mark.parametrize("limit", [0, -1, True])
def test_null_search_rejects_non_positive_limits(limit):
    from gaohe.providers import NullSearchProvider

    with pytest.raises(ValueError, match="Search limit"):
        NullSearchProvider().search("bounded query", limit=limit)
