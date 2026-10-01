"""RSS feed -> watch -> analyze (fake Gemini) -> local page, all through the real modules."""

from datetime import datetime, timezone
from http.client import HTTPConnection
from threading import Thread

from conftest import FakeGemini, FakeTransport, html_response, rss
from gaohe.config import Settings
from gaohe.domain import Source
from gaohe.monitor import watch_once
from gaohe.pipeline import run_pending_analysis
from gaohe.providers import DirectPageFetcher, GeminiAnalysisProvider, GeminiEvidenceAssessor, GeminiSearchProvider
from gaohe.sources import HttpResponse
from gaohe.web import start_server

SETTINGS = Settings(llm_provider="gemini", llm_model="m", llm_api_key="AIza-e2e-secret")
FEED, ARTICLE, NOTICE = "https://a.example/rss", "https://a.example/news/1?session=S", "https://gov.example/notice"


def test_a_monitored_article_becomes_one_visible_mark_on_the_local_page(store):
    store.add_source(Source(None, "甲報", FEED))
    transport = FakeTransport({
        FEED: HttpResponse(200, FEED, {"Content-Type": "application/rss+xml"}, rss(("開放觀光", ARTICLE, None))),
        ARTICLE: html_response(ARTICLE, "開放觀光", "行政院宣布外籍旅客下月起開放入境觀光。"),
        NOTICE: html_response(NOTICE, "公告", "外籍旅客明年三月起開放入境。"),
    })
    watch_once(SETTINGS, store, transport, datetime(2026, 10, 1, tzinfo=timezone.utc), sleep=lambda _s: None)

    gemini = FakeGemini(
        analysis={
            "claims": [{"quote": "外籍旅客下月起開放入境觀光", "kind": "checkable", "materiality": "material"}],
            "candidates": [{"claim_index": 0, "finding_type": "factual_contradiction", "summary": "開放時間", "query": "外籍旅客 開放時間"}],
        },
        assessment={"evidence_quote": "明年三月起開放入境", "rationale": "官方公告時間不同", "relation": "contradicts"},
        search_uris=[NOTICE],
    )
    summary = run_pending_analysis(
        store, GeminiAnalysisProvider(SETTINGS, gemini), GeminiSearchProvider(SETTINGS, gemini),
        DirectPageFetcher(transport), GeminiEvidenceAssessor(SETTINGS, gemini),
    )
    assert (summary["analyzed"], summary["visible_findings"]) == (1, 1)
    assert {call[2] for call in gemini.calls} == {"AIza-e2e-secret"}  # the key goes only to Gemini

    server = start_server(SETTINGS, store)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/")
        page = connection.getresponse().read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
    assert "mark-factual_contradiction" in page and "官方公告時間不同" in page
    assert "AIza-e2e-secret" not in page and "session=S" not in page
