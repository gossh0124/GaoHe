"""Round-2 pipeline behaviour: run-stopping and temporary provider errors, revision_ids, request
metering, per-revision caps, and topic decisions as analysis context."""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from gaohe.domain import (
    AnalysisResult,
    ArticleCandidate,
    Claim,
    EvidenceAssessment,
    FetchedArticle,
    FindingCandidate,
    RetrievedPage,
    SearchHit,
    Source,
    article_content_hash,
)
from gaohe.errors import PROVIDER_ERROR_MESSAGES, ProviderError
from gaohe.pipeline import MAX_RELATED_PEERS, empty_summary, run_pending_analysis
from gaohe.storage import ABANDONED_RUN_ERROR, Store


TAIPEI = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 29, 0, 30, tzinfo=TAIPEI)
FETCHED_AT = "2026-09-28T12:00:00Z"
TEXTS = (
    "Keelung Harbor Bureau renewed twelve ferry permits after inspection. Operators welcomed it.",
    "Nantou County opened a library wing with donated books. Pupils celebrated.",
    "Kaohsiung City Council debated weekend student bus fares. Commuters waited.",
)
FORUM = "Taipei Defense Ministry forum opens with {} troops; reported figure."


def _summary(**counts):
    return empty_summary() | counts


def _save(store: Store, name: str, text: str, *, title: str = "Local news", published_at=None, fetched_at=FETCHED_AT) -> int:
    source_id = store.add_source(Source(None, name, f"https://{name}.test/feed"))
    candidate = ArticleCandidate(source_id, f"https://{name}.test/story", title, published_at, fetched_at, {})
    return store.save_fetched_article(FetchedArticle(candidate, text, fetched_at, article_content_hash(title, text)))[0]


def _store(tmp_path, texts=TEXTS) -> Store:
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    for index, text in enumerate(texts):
        _save(store, f"outlet{index}", text)
    return store


def _ledger(store: Store) -> list[tuple]:
    with sqlite3.connect(store.path) as connection:
        return connection.execute("SELECT purpose, revision_id, status FROM llm_calls ORDER BY id").fetchall()


class FakeAnalysis:
    provider_name = "fake-llm"
    model = "fake-model"

    def __init__(self, *, errors=None, candidate_type=None, requests_per_call=1) -> None:
        self.errors = errors or {}
        self.candidate_type = candidate_type
        self.requests_per_call = requests_per_call
        self.requests_sent = 0
        self.calls: list[int] = []
        self.related: list[tuple[int, ...]] = []

    def analyze(self, revision, related):
        self.calls.append(revision.id)
        self.related.append(tuple(item.id for item in related))
        if self.requests_per_call is not None:
            self.requests_sent += self.requests_per_call
        error = self.errors.get(revision.id)
        if error is not None:
            raise error
        text = revision.text.split(". ")[0]
        claim = Claim(None, revision.id, text, 0, len(text), "checkable", "material", "extracted")
        candidates = ()
        if self.candidate_type:
            candidates = (FindingCandidate(None, self.candidate_type, "Check it", 0, len(text), "material", "permit register"),)
        return AnalysisResult(revision.id, (claim,), candidates)


class FakeSearch:
    def __init__(self, *, error=None, metered=False) -> None:
        self.error = error
        self.calls = 0
        if metered:
            self.provider_name, self.model, self.requests_sent = "fake-search", "search-model", 0

    def search(self, query, limit=5):
        self.calls += 1
        if hasattr(self, "requests_sent"):
            self.requests_sent += 1
        if self.error is not None:
            raise self.error
        return (SearchHit("https://register.test/ferry", "Register", "snippet only", "official", None),)


class PageFetcher:
    def fetch(self, url):
        return RetrievedPage(url, "Register", "The register lists three permits.", "2026-09-28T13:00:00Z", "retrieved", "h")


class Assessor:
    provider_name = "fake-llm"
    model = "fake-assessor"

    def __init__(self, *, error=None) -> None:
        self.error = error
        self.calls: list[int] = []

    def assess(self, claim_text, revision, evidence, finding_type):
        self.calls.append(revision.id)
        if self.error is not None:
            raise self.error
        return EvidenceAssessment("contradicts", "The source gives a different count.", evidence.excerpt[:20])


# --- run-stopping and temporary provider errors (findings 36) ---------------------------------------------------

def test_a_rejected_key_stops_the_run_without_using_attempts(tmp_path):
    store = _store(tmp_path)
    analysis = FakeAnalysis(errors={2: ProviderError("auth", provider="gemini")})

    summary = run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW)

    assert summary == _summary(claims=1, analyzed=1, stopped=1, stop_code="auth")
    assert analysis.calls == [1, 2]
    stopped = store.analysis_status(2)
    assert (stopped["status"], stopped["attempts"], stopped["last_error"]) == ("pending", 0, PROVIDER_ERROR_MESSAGES["auth"])
    assert store.analysis_status(3) is None  # the rest of the queue is untouched
    assert [item.id for item in store.list_pending_revisions(now=NOW.astimezone(timezone.utc))] == [2, 3]


@pytest.mark.parametrize("code", ["quota", "model_not_found", "missing_package", "config"])
def test_every_run_stopping_code_from_the_assessor_also_stops_the_run(tmp_path, code):
    store = _store(tmp_path, TEXTS[:2])
    assessor = Assessor(error=ProviderError(code))

    summary = run_pending_analysis(
        store, FakeAnalysis(candidate_type="factual_contradiction"), FakeSearch(), PageFetcher(), 10, assessor=assessor, now=NOW,
    )

    assert (summary["stopped"], summary["stop_code"], summary["failed"], summary["analyzed"]) == (1, code, 0, 0)
    assert assessor.calls == [1]
    assert store.analysis_status(1)["status"] == "pending" and store.analysis_status(1)["attempts"] == 0
    assert store.list_findings(visible_only=False) == []


def test_a_search_provider_rejecting_the_key_stops_the_run(tmp_path):
    store = _store(tmp_path, TEXTS[:2])

    summary = run_pending_analysis(
        store, FakeAnalysis(candidate_type="factual_contradiction"), FakeSearch(error=ProviderError("auth"), metered=True),
        PageFetcher(), 10, now=NOW,
    )

    assert (summary["stopped"], summary["stop_code"]) == (1, "auth")
    assert [row[0] for row in _ledger(store)] == ["analysis", "search"]


@pytest.mark.parametrize("code", ["rate_limit", "network", "timeout", "unavailable"])
def test_temporary_provider_errors_defer_the_revision_without_an_attempt(tmp_path, code):
    store = _store(tmp_path, TEXTS[:2])
    analysis = FakeAnalysis(errors={1: ProviderError(code)})

    summary = run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW)

    assert summary == _summary(claims=1, analyzed=1, skipped=1)
    deferred = store.analysis_status(1)
    assert (deferred["status"], deferred["attempts"], deferred["last_error"]) == ("skipped", 0, PROVIDER_ERROR_MESSAGES[code])
    analysis.errors.clear()
    assert run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW) == _summary(claims=1, analyzed=1)


def test_a_refusal_fails_the_revision_with_a_plain_reason_and_no_finding(tmp_path):
    store = _store(tmp_path, TEXTS[:1])

    summary = run_pending_analysis(
        store, FakeAnalysis(errors={1: ProviderError("blocked")}), FakeSearch(), PageFetcher(), 10, now=NOW,
    )

    assert summary == _summary(failed=1)
    failed = store.analysis_status(1)
    assert (failed["status"], failed["attempts"], failed["last_error"]) == ("failed", 1, PROVIDER_ERROR_MESSAGES["blocked"])
    assert store.list_findings(visible_only=False) == []


# --- revision_ids --------------------------------------------------------------------------------------------------

def test_revision_ids_limit_the_run_to_those_revisions(tmp_path):
    store = _store(tmp_path)
    analysis = FakeAnalysis()

    summary = run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW, revision_ids=[3])

    assert summary == _summary(claims=1, analyzed=1)
    assert analysis.calls == [3]
    assert store.analysis_status(1) is None and store.analysis_status(2) is None
    assert run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW, revision_ids=[3]) == _summary()


# --- finding 7: the ledger and the budget count requests actually sent ------------------------------------------------

def test_retried_requests_are_ledgered_and_count_towards_the_daily_limit(tmp_path):
    store = _store(tmp_path, TEXTS[:2])
    analysis = FakeAnalysis(requests_per_call=3)  # two retries before the answer

    summary = run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW, daily_llm_call_limit=3)

    assert summary == _summary(claims=1, analyzed=1, skipped=1)
    assert _ledger(store) == [("analysis", 1, "retry"), ("analysis", 1, "retry"), ("analysis", 1, "ok")]
    assert store.count_llm_calls_since("2026-09-28T16:00:00Z") == 3


def test_a_call_that_sent_no_request_is_not_ledgered(tmp_path):
    store = _store(tmp_path, TEXTS[:1])
    analysis = FakeAnalysis(errors={1: ProviderError("missing_package")}, requests_per_call=0)

    summary = run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW)

    assert (summary["stopped"], summary["stop_code"]) == (1, "missing_package")
    assert _ledger(store) == []


def test_metered_search_requests_are_ledgered_per_revision(tmp_path):
    store = _store(tmp_path, TEXTS[:1])
    search = FakeSearch(metered=True)

    run_pending_analysis(store, FakeAnalysis(candidate_type="factual_contradiction"), search, PageFetcher(), 10, now=NOW)

    assert _ledger(store) == [("analysis", 1, "ok"), ("search", 1, "ok")]


# --- finding 16: provider cross-media proposals are not searched, assessed or stored --------------------------------

def test_provider_proposed_cross_media_differences_are_dropped(tmp_path):
    store = _store(tmp_path, TEXTS[:1])
    search, assessor = FakeSearch(), Assessor()

    summary = run_pending_analysis(
        store, FakeAnalysis(candidate_type="material_cross_media_difference"), search, PageFetcher(), 10,
        assessor=assessor, now=NOW,
    )

    assert summary == _summary(claims=1, analyzed=1)
    assert (search.calls, assessor.calls) == (0, [])
    assert store.list_findings(visible_only=False) == []


# --- finding 43: popular stories do not multiply assessments --------------------------------------------------------

def test_peers_and_topic_assessments_are_capped_per_revision(tmp_path):
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    # Twelve outlets on one forum: half report 100 troops, half 1000.
    for index in range(12):
        _save(store, f"outlet{index}", FORUM.format(100 if index % 2 else 1000), title="Taipei defense forum")
    analysis, assessor = FakeAnalysis(), Assessor()

    summary = run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 20, assessor=assessor, now=NOW)

    assert summary["analyzed"] == 12
    assert max(len(related) for related in analysis.related) == MAX_RELATED_PEERS
    # One assessment per differing span of each revision, not one per differing peer (6 each).
    assert sorted(assessor.calls) == list(range(1, 13))


# --- findings 0 and 2 through the pipeline ------------------------------------------------------------------------------

def test_a_run_whose_claim_was_taken_over_saves_nothing(tmp_path):
    store = _store(tmp_path, TEXTS[:1])

    class SlowAnalysis(FakeAnalysis):
        def analyze(self, revision, related):
            # While this run waits on the AI service, a later run decides it crashed and takes the job.
            assert store.mark_analysis_running(revision.id, "2026-09-28T19:00:00Z")
            return super().analyze(revision, related)

    summary = run_pending_analysis(store, SlowAnalysis(), FakeSearch(), PageFetcher(), 10, now=NOW)

    assert summary == _summary()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone() == (0,)
    assert store.analysis_status(1)["status"] == "running"


def test_a_crashed_job_without_attempts_left_is_failed_before_listing(tmp_path):
    store = _store(tmp_path, TEXTS[:1])
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO revision_analysis (revision_id, status, attempts, updated_at) VALUES (1, 'running', 2, ?)",
            ("2026-09-28T12:00:00Z",),
        )
    analysis = FakeAnalysis()

    assert run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW) == _summary()

    assert analysis.calls == []
    status = store.analysis_status(1)
    assert (status["status"], status["attempts"], status["last_error"]) == ("failed", 3, ABANDONED_RUN_ERROR)


# --- findings 1, 5, 46: topic decisions and event time as analysis context ---------------------------------------------

def _forum_pair(tmp_path, **options) -> Store:
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    _save(store, "alpha", FORUM.format(100), title="Taipei defense forum", **options.get("alpha", {}))
    _save(store, "bravo", FORUM.format(1000), title="Taipei defense forum", **options.get("bravo", {}))
    return store


def _new_revision(store: Store, name: str, text: str) -> int:
    source_id = next(source.id for source in store.list_sources() if source.name == name)
    candidate = ArticleCandidate(source_id, f"https://{name}.test/story", "Taipei defense forum", None, FETCHED_AT, {})
    title = "Taipei defense forum"
    return store.save_fetched_article(FetchedArticle(candidate, text, "2026-09-28T13:00:00Z", article_content_hash(title, text)))[0]


def test_a_dismissed_pair_stays_apart_after_either_article_changes(tmp_path):
    store = _forum_pair(tmp_path)
    topic_id = store.assign_topic(1, 2, "Taipei defense forum", "high")
    store.set_topic_status(topic_id, "dismissed")
    bravo_update = _new_revision(store, "bravo", FORUM.format(1000) + " More delegates arrived.")
    analysis, assessor = FakeAnalysis(), Assessor()

    run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, assessor=assessor, now=NOW)

    assert analysis.calls == [1, bravo_update] and analysis.related == [(), ()]
    assert assessor.calls == [] and store.list_findings() == []
    assert store.dashboard_snapshot()["comparisons"] == []


def test_a_reviewer_confirmed_possible_topic_becomes_analysis_context(tmp_path):
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    # Two stories on one host only ever group as possible.
    source_id = store.add_source(Source(None, "alpha", "https://alpha.test/feed"))
    for path, count in (("forum", 100), ("forum-update", 1000)):
        text = FORUM.format(count)
        candidate = ArticleCandidate(source_id, f"https://alpha.test/{path}", "Taipei defense forum", None, FETCHED_AT, {})
        store.save_fetched_article(FetchedArticle(candidate, text, FETCHED_AT, article_content_hash(candidate.title, text)))
    analysis = FakeAnalysis()
    run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW)
    assert analysis.related == [(), ()]
    topic_id = store.topic_id_for_revision(1)
    assert store.set_topic_status(topic_id, "active")

    changed = FORUM.format(1000) + " Update."
    candidate = ArticleCandidate(source_id, "https://alpha.test/forum-update", "Taipei defense forum", None, FETCHED_AT, {})
    update = store.save_fetched_article(FetchedArticle(candidate, changed, FETCHED_AT, article_content_hash(candidate.title, changed)))[0]
    run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW)

    assert analysis.calls[-1] == update and analysis.related[-1] == (1,)
    assert store.topic_id_for_revision(update) == topic_id


def test_articles_published_weeks_apart_are_not_grouped_even_when_fetched_together(tmp_path):
    store = _forum_pair(tmp_path, alpha={"published_at": "2026-08-07T01:00:00Z"}, bravo={"published_at": "2026-09-03T01:00:00Z"})
    analysis = FakeAnalysis()

    run_pending_analysis(store, analysis, FakeSearch(), PageFetcher(), 10, now=NOW)

    assert analysis.related == [(), ()]
    assert store.dashboard_snapshot()["comparisons"] == []
