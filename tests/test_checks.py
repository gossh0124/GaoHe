"""checks.check_article_url with fake fetcher, providers and DNS resolver (no network, no real AI)."""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import gaohe.providers as providers
from gaohe.checks import (
    FETCH_FAILED_MESSAGE,
    INVALID_URL_MESSAGE,
    PRIVATE_URL_MESSAGE,
    SETUP_MISSING_MESSAGE,
    check_article_url,
)
from gaohe.config import Settings
from gaohe.domain import AnalysisResult, Claim, EvidenceAssessment, FindingCandidate, RetrievedPage, SearchHit
from gaohe.errors import PROVIDER_ERROR_MESSAGES, ProviderError
from gaohe.storage import MANUAL_SOURCE_NAME, Store


NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone(timedelta(hours=8)))
ARTICLE_URL = "https://News.Example/2026/ferry?id=7#comments"
STORED_URL = "https://news.example/2026/ferry?id=7"
ARTICLE_TEXT = "基隆港務局今天核發十二張渡輪執照，業者表示歡迎。"
REGISTER_URL = "https://register.example/ferry"
REGISTER_TEXT = "基隆港務局公告：本季只核發三張渡輪執照。"
KEY = "sample-user-key-123"


def public_resolver(host, port):
    del port
    return [(2, 1, 6, "", ("93.184.216.34" if host != "intranet.example" else "10.0.0.8", 0))]


class FakeFetcher:
    def __init__(self, pages=None, *, fail=False) -> None:
        self.pages = pages if pages is not None else {
            STORED_URL: ("基隆渡輪執照", ARTICLE_TEXT),
            REGISTER_URL: ("港務局公告", REGISTER_TEXT),
        }
        self.fail = fail
        self.calls: list[str] = []

    def fetch(self, url):
        self.calls.append(url)
        if self.fail:
            raise TimeoutError("slow")
        if url not in self.pages:
            return RetrievedPage(url, "", "", "2026-09-29T02:00:00Z", "http_error", None)
        title, text = self.pages[url]
        return RetrievedPage(url, title, text, "2026-09-29T02:00:00Z", "retrieved", "hash", fetched_via="direct")


class FakeAnalysis:
    provider_name = "fake-llm"
    model = "fake-model"

    def __init__(self, *, error=None) -> None:
        self.error = error
        self.calls: list[int] = []

    def analyze(self, revision, related):
        self.calls.append(revision.id)
        if self.error is not None:
            raise self.error
        quote = "核發十二張渡輪執照"
        start = revision.text.index(quote)
        claim = Claim(None, revision.id, quote, start, start + len(quote), "checkable", "material", "extracted")
        candidate = FindingCandidate(
            None, "factual_contradiction", "執照數量與公告不符", start, start + len(quote), "material", "基隆 渡輪 執照 公告",
        )
        return AnalysisResult(revision.id, (claim,), (candidate,))


class FakeSearch:
    def search(self, query, limit=5):
        del query, limit
        return (SearchHit(REGISTER_URL, "港務局公告", "只是摘要", "official", None),)


class ContradictsAssessor:
    provider_name = "fake-llm"
    model = "fake-assessor"

    def __init__(self) -> None:
        self.calls = 0

    def assess(self, claim_text, revision, evidence, finding_type):
        self.calls += 1
        return EvidenceAssessment("contradicts", "公告寫本季只核發三張。", "本季只核發三張渡輪執照")


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(llm_provider="gemini", llm_model="gemini-2.5-flash", llm_api_key=KEY, data_dir=tmp_path)
    values.update(overrides)
    return Settings(**values)


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    return store


def _check(tmp_path, store, url=ARTICLE_URL, **options):
    options.setdefault("fetcher", FakeFetcher())
    options.setdefault("analysis", FakeAnalysis())
    options.setdefault("search", FakeSearch())
    options.setdefault("assessor", ContradictsAssessor())
    options.setdefault("resolver", public_resolver)
    return check_article_url(options.pop("settings", _settings(tmp_path)), store, url, now=NOW, **options)


@pytest.mark.parametrize("url", ["", "news.example/story", "ftp://news.example/a", "https://news.example/a b",
                                 "https://user:pass@news.example/a", "javascript:alert(1)", None])
def test_invalid_urls_are_refused_before_any_fetch(tmp_path, url):
    store = _store(tmp_path)
    fetcher = FakeFetcher()

    outcome = _check(tmp_path, store, url, fetcher=fetcher)

    assert (outcome.status, outcome.message, outcome.revision_id) == ("invalid_url", INVALID_URL_MESSAGE, None)
    assert fetcher.calls == []


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/", "http://localhost/a", "http://intranet.example/a", "http://[::1]/"])
def test_private_hosts_are_refused_before_any_fetch(tmp_path, url):
    store = _store(tmp_path)
    fetcher = FakeFetcher()

    outcome = _check(tmp_path, store, url, fetcher=fetcher)

    assert (outcome.status, outcome.message) == ("invalid_url", PRIVATE_URL_MESSAGE)
    assert fetcher.calls == []


@pytest.mark.parametrize("fetcher", [FakeFetcher(fail=True), FakeFetcher(pages={})])
def test_unreadable_page_is_a_fetch_failure_not_a_verdict(tmp_path, fetcher):
    store = _store(tmp_path)
    analysis = FakeAnalysis()

    outcome = _check(tmp_path, store, fetcher=fetcher, analysis=analysis)

    assert (outcome.status, outcome.message, outcome.revision_id) == ("fetch_failed", FETCH_FAILED_MESSAGE, None)
    assert analysis.calls == []
    assert store.dashboard_snapshot()["inbox"] == []


def test_a_page_that_redirected_to_a_private_address_is_not_saved(tmp_path):
    store = _store(tmp_path)

    class Redirected(FakeFetcher):
        def fetch(self, url):
            page = super().fetch(url)
            return RetrievedPage("http://intranet.example/admin", page.title, page.text, page.retrieved_at, page.status, None)

    outcome = _check(tmp_path, store, fetcher=Redirected())

    assert outcome.status == "fetch_failed"
    assert store.dashboard_snapshot()["inbox"] == []


def test_completed_check_saves_a_manual_article_with_a_visible_finding(tmp_path):
    store = _store(tmp_path)
    assessor = ContradictsAssessor()

    outcome = _check(tmp_path, store, assessor=assessor)

    assert outcome.status == "completed"
    assert "有 1 處形成標註" in outcome.message and KEY not in outcome.message
    assert outcome.revision_id is not None and outcome.article_id is not None
    detail = store.article_detail(outcome.revision_id)
    assert (detail["source"], detail["manual"], detail["url"], detail["text"]) == (
        MANUAL_SOURCE_NAME, True, STORED_URL, ARTICLE_TEXT,
    )
    [finding] = detail["findings"]
    assert (finding["finding_type"], finding["visible"], finding["evidence"][0]["source_kind"]) == (
        "factual_contradiction", True, "direct",
    )
    assert store.dashboard_snapshot()["sources"] == []  # the manual source is never listed or polled
    assert assessor.calls == 1


def test_checking_the_same_unchanged_url_again_makes_no_new_ai_call(tmp_path):
    store = _store(tmp_path)
    analysis, assessor = FakeAnalysis(), ContradictsAssessor()
    first = _check(tmp_path, store, analysis=analysis, assessor=assessor)

    again = _check(tmp_path, store, analysis=analysis, assessor=assessor)

    assert (again.status, again.revision_id, again.article_id) == ("completed", first.revision_id, first.article_id)
    assert again.message.startswith("這篇文章內容沒有變更")
    assert analysis.calls == [first.revision_id] and assessor.calls == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM llm_calls").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM findings").fetchone() == (1,)


def test_changed_text_is_analyzed_as_a_new_revision(tmp_path):
    store = _store(tmp_path)
    analysis = FakeAnalysis()
    first = _check(tmp_path, store, analysis=analysis)
    corrected = FakeFetcher({STORED_URL: ("基隆渡輪執照", ARTICLE_TEXT + "（更正：核發三張）"), REGISTER_URL: ("公告", REGISTER_TEXT)})

    second = _check(tmp_path, store, analysis=analysis, fetcher=corrected)

    assert second.status == "completed" and second.revision_id != first.revision_id
    assert second.article_id == first.article_id
    assert store.article_detail(first.revision_id)["is_current"] is False


def test_rejected_key_stops_with_a_plain_message_and_keeps_the_article_pending(tmp_path):
    store = _store(tmp_path)

    outcome = _check(tmp_path, store, analysis=FakeAnalysis(error=ProviderError("auth", provider="gemini")))

    assert outcome.status == "failed"
    assert outcome.message.startswith(PROVIDER_ERROR_MESSAGES["auth"])
    status = store.analysis_status(outcome.revision_id)
    assert (status["status"], status["attempts"]) == ("pending", 0)


def test_temporary_provider_problem_defers_the_check(tmp_path):
    store = _store(tmp_path)

    outcome = _check(tmp_path, store, analysis=FakeAnalysis(error=ProviderError("rate_limit")))

    assert outcome.status == "skipped" and "之後自動分析" in outcome.message
    assert store.analysis_status(outcome.revision_id)["attempts"] == 0


def test_a_failed_article_is_retried_when_the_user_checks_it_again(tmp_path):
    store = _store(tmp_path)
    failed = _check(tmp_path, store, analysis=FakeAnalysis(error=RuntimeError("bad answer")))
    assert failed.status == "failed"
    revision_id = failed.revision_id
    # Scheduled runs then used up the remaining attempts.
    store.mark_analysis_failed(revision_id, "2026-09-29T03:00:00Z", "bad answer", final_attempts=3)
    assert store.list_pending_revisions(now="2026-09-29T03:00:00Z") == []

    outcome = _check(tmp_path, store)

    assert (outcome.status, outcome.revision_id) == ("completed", revision_id)


def test_missing_setup_saves_the_article_and_explains_what_to_do(tmp_path):
    store = _store(tmp_path)

    outcome = _check(tmp_path, store, settings=_settings(tmp_path, llm_api_key=""), analysis=None, assessor=None, search=None)

    assert (outcome.status, outcome.message) == ("failed", SETUP_MISSING_MESSAGE)
    assert store.article_detail(outcome.revision_id)["analysis_status"] == "unanalyzed"


def test_providers_are_built_from_settings_when_not_injected(tmp_path, monkeypatch):
    store = _store(tmp_path)
    analysis, assessor = FakeAnalysis(), ContradictsAssessor()
    monkeypatch.setattr(providers, "build_analysis_provider", lambda settings: analysis)
    monkeypatch.setattr(providers, "build_evidence_assessor", lambda settings: assessor)
    monkeypatch.setattr(providers, "build_search_provider", lambda settings: FakeSearch())

    outcome = _check(tmp_path, store, analysis=None, assessor=None, search=None)

    assert outcome.status == "completed"
    assert analysis.calls == [outcome.revision_id] and assessor.calls == 1


def test_a_provider_that_cannot_be_built_is_reported_plainly(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def missing(settings):
        raise ProviderError("missing_package", provider="anthropic")

    monkeypatch.setattr(providers, "build_analysis_provider", missing)

    outcome = _check(tmp_path, store, analysis=None)

    assert outcome.status == "failed" and outcome.message.startswith(PROVIDER_ERROR_MESSAGES["missing_package"])


def test_budget_reached_defers_the_check(tmp_path):
    store = _store(tmp_path)
    store.record_llm_call("2026-09-29T01:00:00Z", "fake-llm", "fake-model", "analysis", None, "ok")

    outcome = _check(tmp_path, store, settings=_settings(tmp_path, daily_llm_call_limit=1))

    assert outcome.status == "skipped" and "上限" in outcome.message
