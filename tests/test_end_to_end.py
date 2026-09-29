"""Offline end to end: two Chinese RSS feeds -> watch -> analysis -> local page.

Everything real except the network: a fake transport serves the feeds and
articles, and the Gemini adapters talk to a fake request function, so the
structured-output parsing, quote anchoring, evidence assessment, visibility
policy, storage and rendering all run as in production.
"""

from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
import re
import sqlite3
from threading import Thread

from gaohe import cli
from gaohe.config import load_settings
from gaohe.domain import RetrievedPage, SearchHit, Source
from gaohe.monitor import watch_once
from gaohe.pipeline import SUMMARY_KEYS, run_pending_analysis
from gaohe.providers import GeminiAnalysisProvider, GeminiEvidenceAssessor
from gaohe.sources import HttpResponse
from gaohe.storage import Store
from gaohe.web import start_server


NOW = datetime(2026, 9, 29, 9, 0, tzinfo=timezone(timedelta(hours=8)))
MODEL = "gemini-2.5-flash-lite"
API_KEY = "AIza" + "E2eUserKey" * 3 + "abcde"

FEED_A = "https://alpha-news.test/rss.xml?token=FEED-SECRET-A"
FEED_B = "https://bravo-daily.test/feed.xml"
ARTICLE_A = "https://alpha-news.test/news/1"
ARTICLE_B = "https://bravo-daily.test/story/2?session=ARTICLE-SECRET-B"
BUDGET_URL = "https://gov.test/tourism/budget"
MIRROR_URL = "https://mirror.test/budget?api_key=EVIDENCE-SECRET"
LEAD_URL = "https://leads.test/drill"
URL_SECRETS = ("FEED-SECRET-A", "ARTICLE-SECRET-B", "EVIDENCE-SECRET")

TEXT_A = ("行政院今天宣布開放外籍旅客入境觀光，首批旅行團共有500人抵達桃園機場。", "交通部表示，今年觀光預算為30億元，將用於國際行銷。")
TEXT_B = ("行政院宣布開放外籍旅客入境觀光後，首批旅行團共5000人抵達桃園機場。", "觀光署指出，旅行業者已完成防疫演練，機場也增設快篩站。")
BUDGET_PAGE = "交通部公布今年觀光預算為12億元，較去年增加。"
BUDGET_SUMMARY = "文章所稱今年觀光預算30億元，需與交通部公告核對。"
BUDGET_RATIONALE = "交通部公告記載今年觀光預算為12億元，與文章所稱30億元不同。"
PENDING_SUMMARY = "需確認旅行業者是否已完成防疫演練。"
LEAD_SNIPPET = "搜尋摘要：演練延期"
TOPIC_RATIONALE = "另一家媒體報導的首批旅客人數不同，差距達十倍。"


def _rss(title: str, items: tuple[tuple[str, str], ...]) -> bytes:
    entries = "".join(
        f"<item><title>{item_title}</title><link>{link}</link><pubDate>2026-09-29T00:30:00Z</pubDate></item>"
        for item_title, link in items
    )
    return f"<rss><channel><title>{title}</title>{entries}</channel></rss>".encode("utf-8")


def _article(title: str, paragraphs: tuple[str, ...]) -> bytes:
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return f"<html><head><title>{title}</title></head><body><main>{body}</main></body></html>".encode("utf-8")


class FakeTransport:
    """Serves fixed responses; matches a URL exactly, else without its query string."""

    def __init__(self) -> None:
        rss, html = "application/rss+xml; charset=utf-8", "text/html; charset=utf-8"
        self.responses = {
            FEED_A: (rss, _rss("甲報", (("政院開放外籍旅客入境", ARTICLE_A),))),
            FEED_B: (rss, _rss("乙報", (("外籍旅客入境觀光首發團抵台", ARTICLE_B),))),
            ARTICLE_A: (html, _article("政院開放外籍旅客入境", TEXT_A)),
            ARTICLE_B: (html, _article("外籍旅客入境觀光首發團抵台", TEXT_B)),
        }

    def fetch(self, url, *args, **kwargs):
        del args, kwargs
        found = self.responses.get(url) or self.responses.get(url.split("?", 1)[0])
        if found is None:
            return HttpResponse(404, url, {"Content-Type": "text/plain"}, b"")
        content_type, body = found
        return HttpResponse(200, url, {"Content-Type": content_type}, body)


def _claim(quote: str) -> dict[str, object]:
    return {"quote": quote, "kind": "checkable", "materiality": "material"}


class FakeGemini:
    """Stands in for generateContent; answers from the request document like a model would."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.kinds: list[str] = []

    def __call__(self, model, payload, api_key):
        assert (model, api_key) == (MODEL, API_KEY)  # the key only travels in the request seam
        document = json.loads(payload["contents"][0]["parts"][0]["text"])
        if "claims" in payload["generationConfig"]["responseSchema"]["properties"]:
            self.kinds.append("analysis")
            return json.dumps(self._analysis(document["revision"]["text"]), ensure_ascii=False)
        self.kinds.append("assessment")
        return json.dumps(self._assessment(document), ensure_ascii=False)

    def _analysis(self, text: str) -> dict[str, object]:
        if self.fail_on and self.fail_on in text:
            raise RuntimeError(f"upstream echoed key={API_KEY}")
        if "30億元" in text:
            claims = [_claim("首批旅行團共有500人抵達桃園機場"), _claim("今年觀光預算為30億元")]
            candidate = {"summary": BUDGET_SUMMARY, "query": "交通部 今年觀光預算"}
        else:
            claims = [_claim("首批旅行團共5000人抵達桃園機場"), _claim("旅行業者已完成防疫演練")]
            candidate = {"summary": PENDING_SUMMARY, "query": "旅行業者 防疫演練"}
        candidate.update({"claim_index": 1, "finding_type": "factual_contradiction", "materiality": "material"})
        return {"claims": claims, "candidates": [candidate]}

    @staticmethod
    def _assessment(document: dict[str, object]) -> dict[str, object]:
        excerpt = document["evidence"]["excerpt"]
        if document["finding_type"] == "factual_contradiction":
            return {"evidence_quote": "今年觀光預算為12億元", "rationale": BUDGET_RATIONALE, "relation": "contradicts"}
        quote = next(part for part in re.split("[。\n]", excerpt) if "抵達桃園機場" in part)
        return {"evidence_quote": quote, "rationale": TOPIC_RATIONALE, "relation": "contradicts"}


class FakeSearch:
    def search(self, query, limit=5):
        if "觀光預算" in query:
            return (
                SearchHit(BUDGET_URL, "交通部觀光預算公告", "預算摘要", "official", None),
                SearchHit(MIRROR_URL, "轉載", "轉載摘要", "mirror", None),
            )
        return (SearchHit(LEAD_URL, "防疫演練", LEAD_SNIPPET, "search", None),)


class FakeFetcher:
    def fetch(self, url):
        if url == BUDGET_URL:
            return RetrievedPage(url, "交通部觀光預算公告", BUDGET_PAGE, "2026-09-29T01:00:00Z", "retrieved", "hash", "direct")
        return RetrievedPage(url, "", "", "2026-09-29T01:00:00Z", "http_error", None)


def _setup(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LLM_PROVIDER=gemini\nLLM_MODEL={MODEL}\nLLM_API_KEY={API_KEY}\nDATA_DIR={tmp_path / 'data'}\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file, environ={})
    store = Store(settings.database_path)
    store.initialize()
    store.add_source(Source(None, "甲報", FEED_A))
    store.add_source(Source(None, "乙報", FEED_B))
    summary = watch_once(settings, store, FakeTransport(), now=NOW)
    assert (summary.sources_checked, summary.revisions_created, summary.failures) == (2, 2, 0)
    return env_file, settings, store


def _analyze(settings, store, gemini: FakeGemini) -> dict[str, int]:
    return run_pending_analysis(
        store,
        GeminiAnalysisProvider(settings, request=gemini, sleep=lambda seconds: None),
        FakeSearch(),
        FakeFetcher(),
        10,
        assessor=GeminiEvidenceAssessor(settings, request=gemini, sleep=lambda seconds: None),
        now=NOW,
        daily_llm_call_limit=settings.daily_llm_call_limit,
    )


def _get_page(settings, store) -> str:
    server = start_server(settings, store)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        return response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _section(page: str, anchor: str) -> str:
    start = page.index(f"<section id='{anchor}'")
    return page[start:page.index("</section>", start)]


def _assert_no_secrets(text: str) -> None:
    assert API_KEY not in text
    for secret in URL_SECRETS:
        assert secret not in text


def test_two_chinese_feeds_become_assessed_visible_findings_on_the_local_page(tmp_path, capsys):
    env_file, settings, store = _setup(tmp_path)
    gemini = FakeGemini()

    summary = _analyze(settings, store, gemini)

    assert summary == dict(zip(SUMMARY_KEYS, (4, 4, 3, 1, 2, 0, 2, 0, 0)))
    assert gemini.kinds.count("analysis") == 2 and gemini.kinds.count("assessment") == 3
    with sqlite3.connect(store.path) as connection:
        ledger = connection.execute("SELECT provider, model, purpose, status FROM llm_calls ORDER BY id").fetchall()
    assert sorted(ledger) == sorted([("gemini", MODEL, "analysis", "ok")] * 2 + [("gemini", MODEL, "assessment", "ok")] * 3)

    page = _get_page(settings, store)

    findings = _section(page, "findings")
    assert findings.count("<li class='finding'>") == 3
    assert findings.count(">事實矛盾</span>") == 1
    assert findings.count(">實質跨媒體差異</span>") == 2
    assert f"判讀理由：{BUDGET_RATIONALE}" in findings
    assert f"判讀理由：{TOPIC_RATIONALE}" in findings
    assert "找到反向證據" in findings and "待人工確認" in findings
    inbox = _section(page, "inbox")
    assert "<mark class='annotation mark-factual_contradiction'" in inbox
    assert "<mark class='annotation mark-material_cross_media_difference'" in inbox
    assert "1 項候選問題待補查" in inbox
    assert "已完成分析" in inbox
    # The snippet-only candidate is only counted as pending: no text, no mark, no snippet.
    assert PENDING_SUMMARY not in page and LEAD_SNIPPET not in page
    assert "同題" in _section(page, "comparisons")
    _assert_no_secrets(page)

    database = b"".join(path.read_bytes() for path in settings.data_dir.glob("gaohe.db*"))
    assert API_KEY.encode() not in database and b"EVIDENCE-SECRET" not in database

    assert cli.main(["finding", "list", "--all", "--env-file", str(env_file)]) == 0
    assert cli.main(["status", "--env-file", str(env_file)]) == 0
    output = capsys.readouterr().out
    assert output.count("visible=yes") == 3 and output.count("visible=no") == 1
    assert "llm_calls_today=5" in output and "completed=2" in output
    _assert_no_secrets(output)


def test_provider_failure_for_one_revision_leaves_the_other_completed(tmp_path):
    _, settings, store = _setup(tmp_path)

    summary = _analyze(settings, store, FakeGemini(fail_on="5000人"))

    assert (summary["analyzed"], summary["failed"], summary["visible_findings"]) == (1, 1, 2)
    statuses = sorted(store.analysis_status(revision.id)["status"] for revision in store.list_recent_revisions())
    assert statuses == ["completed", "failed"]
    failed = next(
        status for revision in store.list_recent_revisions()
        if (status := store.analysis_status(revision.id))["status"] == "failed"
    )
    assert (failed["attempts"], failed["last_error"]) == (1, "ValueError: Gemini analysis request failed")

    page = _get_page(settings, store)

    assert "分析失敗" in _section(page, "inbox")
    assert _section(page, "findings").count("<li class='finding'>") == 2
    _assert_no_secrets(page)


def test_reached_budget_defers_both_revisions_to_a_later_run(tmp_path):
    _, settings, store = _setup(tmp_path)
    gemini = FakeGemini()

    summary = run_pending_analysis(
        store,
        GeminiAnalysisProvider(settings, request=gemini, sleep=lambda seconds: None),
        FakeSearch(),
        FakeFetcher(),
        10,
        assessor=GeminiEvidenceAssessor(settings, request=gemini, sleep=lambda seconds: None),
        now=NOW,
        daily_llm_call_limit=1,
    )

    assert (summary["analyzed"], summary["skipped"]) == (0, 2)
    assert gemini.kinds == ["analysis"]
    assert sorted(store.analysis_counts().items()) == sorted(
        {"pending": 0, "running": 0, "completed": 0, "failed": 0, "skipped": 2, "unanalyzed": 0}.items()
    )
