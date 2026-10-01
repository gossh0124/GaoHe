from conftest import FakeGemini, FakeTransport, html_response
from gaohe.checks import FETCH_FAILED, INVALID_URL, PRIVATE_URL, SETUP_MISSING, check_article_url
from gaohe.config import Settings
from gaohe.providers import DirectPageFetcher, GeminiAnalysisProvider, GeminiEvidenceAssessor, GeminiSearchProvider

SETTINGS = Settings(llm_provider="gemini", llm_model="m", llm_api_key="k")
URL = "https://news.example/story"
TEXT = "外籍旅客下月起開放入境觀光。"


def providers(gemini):
    return {
        "analysis": GeminiAnalysisProvider(SETTINGS, gemini),
        "search": GeminiSearchProvider(SETTINGS, gemini),
        "assessor": GeminiEvidenceAssessor(SETTINGS, gemini),
    }


def fetcher(**pages):
    return DirectPageFetcher(FakeTransport({URL: html_response(URL, "開放觀光", TEXT), **pages}))


def test_invalid_private_and_unreadable_urls_are_explained_never_judged(store, public_resolver):
    private = lambda host, port: [(2, 1, 6, "", ("10.0.0.2", 0))]  # noqa: E731
    assert check_article_url(SETTINGS, store, "not a url", resolver=public_resolver).message == INVALID_URL
    assert check_article_url(SETTINGS, store, URL, resolver=private).message == PRIVATE_URL
    failed = check_article_url(SETTINGS, store, URL, fetcher=DirectPageFetcher(FakeTransport({})), resolver=public_resolver)
    assert (failed.status, failed.message) == ("fetch_failed", FETCH_FAILED)


def test_missing_setup_keeps_the_article_and_says_what_to_do(store, public_resolver):
    outcome = check_article_url(Settings(), store, URL, fetcher=fetcher(), resolver=public_resolver)
    assert (outcome.status, outcome.message) == ("failed", SETUP_MISSING) and outcome.revision_id
    assert store.dashboard_snapshot()["inbox"][0]["source"] == "單篇查核"


def test_a_check_with_a_visible_finding_and_a_repeat_without_new_ai_calls(store, public_resolver):
    gemini = FakeGemini(
        analysis={
            "claims": [{"quote": "下月起開放入境觀光", "kind": "checkable", "materiality": "material"}],
            "candidates": [{"claim_index": 0, "finding_type": "factual_contradiction", "summary": "開放時間", "query": "外籍旅客 開放時間"}],
        },
        assessment={"evidence_quote": "明年三月起開放", "rationale": "官方公告時間不同", "relation": "contradicts"},
        search_uris=["https://gov.example/notice"],
    )
    pages = {"https://gov.example/notice": html_response("https://gov.example/notice", "公告", "外籍旅客明年三月起開放入境。")}
    first = check_article_url(SETTINGS, store, URL, fetcher=fetcher(**pages), resolver=public_resolver, **providers(gemini))
    assert first.status == "completed" and "有 1 處形成標註" in first.message
    calls = len(gemini.calls)
    again = check_article_url(SETTINGS, store, URL, fetcher=fetcher(**pages), resolver=public_resolver, **providers(gemini))
    assert again.revision_id == first.revision_id and "沒有變更" in again.message
    assert len(gemini.calls) == calls


def test_a_rejected_key_is_reported_and_the_article_stays_queued(store, public_resolver):
    from gaohe.errors import ProviderError

    outcome = check_article_url(SETTINGS, store, URL, fetcher=fetcher(), resolver=public_resolver, **providers(FakeGemini(error=ProviderError("auth"))))
    assert outcome.status == "failed" and "金鑰" in outcome.message
    assert [r.id for r in store.list_pending_revisions()] == [outcome.revision_id]
