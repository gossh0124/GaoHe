from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import gaohe.pipeline as pipeline
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
from gaohe.pipeline import BUDGET_SKIP_REASON, SUMMARY_KEYS, local_day_start, run_pending_analysis
from gaohe.providers import ANALYSIS_PROMPT_VERSION, NullEvidenceAssessor
from gaohe.storage import Store


TAIPEI = timezone(timedelta(hours=8))
# 00:30 on 29 September in Taiwan is still 28 September in UTC.
NOW = datetime(2026, 9, 29, 0, 30, tzinfo=TAIPEI)
LOCAL_MIDNIGHT_UTC = "2026-09-28T16:00:00Z"
FETCHED_AT = "2026-09-28T12:00:00Z"
SECRET = "AIza" + "S" * 35

ARTICLES = (
    ("Harbor", "https://harbor.test/ferry", "Harbor ferry permit",
     "Keelung Harbor Bureau renewed twelve ferry permits after inspection. Operators welcomed it."),
    ("School", "https://school.test/library", "Mountain school library",
     "Nantou County opened a library wing with donated books. Pupils celebrated."),
    ("Council", "https://council.test/fares", "Bus fare debate",
     "Kaohsiung City Council debated weekend student bus fares. Commuters waited."),
)
PAIR = (
    ("Alpha", "https://alpha.test/forum", "Taipei Defense Ministry forum opens with 100 troops; reported figure."),
    ("Bravo", "https://bravo.test/forum", "Taipei Defense Ministry forum opens with 1000 troops; reported figure."),
)
PAGE_TEXT = "The Keelung Harbor Bureau register lists three renewed ferry permits for this season."


def _summary(**counts: int) -> dict[str, int]:
    return {key: counts.get(key, 0) for key in SUMMARY_KEYS}


def _save(store: Store, name: str, url: str, title: str, text: str, fetched_at: str = FETCHED_AT) -> int:
    source_id = store.add_source(Source(None, name, f"https://{name.lower()}.test/feed"))
    candidate = ArticleCandidate(source_id, url, title, None, fetched_at, {})
    revision_id, _ = store.save_fetched_article(
        FetchedArticle(candidate, text, fetched_at, article_content_hash(title, text))
    )
    return revision_id


def _store(tmp_path, articles=ARTICLES) -> Store:
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    for name, url, title, text in articles:
        _save(store, name, url, title, text)
    return store


def _pair_store(tmp_path, urls=None) -> Store:
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    for index, (name, url, text) in enumerate(PAIR):
        _save(store, name, urls[index] if urls else url, "Taipei defense forum", text)
    return store


class FakeAnalysis:
    """Claims each article's first sentence; optionally proposes it as a factual contradiction."""

    provider_name = "fake-llm"
    model = "fake-model-1"

    def __init__(self, *, fail_urls=(), candidate=False, rejected=0, invalid_claim=False) -> None:
        self.fail_urls = set(fail_urls)
        self.candidate = candidate
        self.rejected = rejected
        self.invalid_claim = invalid_claim
        self.calls: list[int] = []
        self.related: list[tuple[int, ...]] = []

    def analyze(self, revision, related):
        self.calls.append(revision.id)
        self.related.append(tuple(item.id for item in related))
        if revision.url in self.fail_urls:
            raise RuntimeError(f"provider exploded key={SECRET}\nsecond line")
        text = revision.text.split(". ")[0]
        claims = [Claim(None, revision.id, text, 0, len(text), "checkable", "material", "extracted")]
        if self.invalid_claim:
            claims.append(Claim(None, revision.id, "not in the article", 0, 18, "checkable", "material", "extracted"))
        candidates = ()
        if self.candidate:
            candidates = (FindingCandidate(
                None, "factual_contradiction", "Check the permit count", 0, len(text), "material", "ferry permit register",
            ),)
        return AnalysisResult(revision.id, tuple(claims), candidates, rejected_claims=self.rejected)


class FakeSearch:
    def __init__(self, urls=()) -> None:
        self.urls = urls

    def search(self, query, limit=5):
        del query
        return tuple(SearchHit(url, "Register", "snippet only", "official", None) for url in self.urls[:limit])


class PageFetcher:
    def __init__(self, text=PAGE_TEXT) -> None:
        self.text = text

    def fetch(self, url):
        return RetrievedPage(url, "Register", self.text, "2026-09-28T13:00:00Z", "retrieved", "hash", fetched_via="direct")


class NoFetcher:
    def fetch(self, url):
        raise AssertionError(f"unexpected fetch {url}")


class ContradictsAssessor:
    provider_name = "fake-llm"
    model = "fake-assessor-1"

    def __init__(self, *, fail=False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int, str]] = []

    def assess(self, claim_text, revision, evidence, finding_type):
        self.calls.append((claim_text, revision.id, evidence.source_kind))
        if self.fail:
            raise RuntimeError("assessor unavailable")
        quote = evidence.excerpt.split(";")[0][:60]
        return EvidenceAssessment("contradicts", "The source gives a different count.", quote)


def _ledger(store: Store) -> list[tuple]:
    with sqlite3.connect(store.path) as connection:
        return connection.execute(
            "SELECT called_at, provider, model, purpose, revision_id, status, input_chars > 0, output_chars > 0 "
            "FROM llm_calls ORDER BY id"
        ).fetchall()


def _record_calls(store: Store, called_at: str, count: int) -> None:
    for _ in range(count):
        store.record_llm_call(called_at, "fake-llm", "fake-model-1", "analysis", None, "ok")


# --- summary and provider path ----------------------------------------------------------------------

def test_summary_reports_every_key_in_order_and_no_article_text(tmp_path):
    store = _store(tmp_path)

    summary = run_pending_analysis(store, FakeAnalysis(), FakeSearch(), NoFetcher(), 10, now=NOW)

    assert list(summary) == list(SUMMARY_KEYS)
    assert summary == _summary(claims=3, analyzed=3)
    assert store.list_pending_revisions(now=NOW.astimezone(timezone.utc)) == []
    assert "Keelung" not in str(summary)


def test_empty_queue_calls_no_provider(tmp_path):
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    analysis = FakeAnalysis()

    assert run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW) == _summary()
    assert analysis.calls == []


def test_rejected_claims_from_provider_and_validation_are_summed(tmp_path):
    store = _store(tmp_path, ARTICLES[:1])

    summary = run_pending_analysis(store, FakeAnalysis(rejected=2, invalid_claim=True), FakeSearch(), NoFetcher(), 10, now=NOW)

    assert summary == _summary(claims=1, rejected_claims=3, analyzed=1)


def test_assessed_full_text_contradiction_is_visible_and_job_metadata_is_saved(tmp_path):
    store = _store(tmp_path, ARTICLES[:1])
    assessor = ContradictsAssessor()

    summary = run_pending_analysis(
        store, FakeAnalysis(candidate=True), FakeSearch(("https://register.test/ferry",)), PageFetcher(), 10,
        assessor=assessor, now=NOW,
    )

    assert summary == _summary(claims=1, candidates=1, visible_findings=1, analyzed=1)
    claim_text = ARTICLES[0][3].split(". ")[0]
    assert assessor.calls == [(claim_text, 1, "direct")]
    [finding] = store.list_findings()
    assert (finding["finding_type"], finding["status"], finding["evidence_status"]) == (
        "factual_contradiction", "resolved", "retrieved",
    )
    assert finding["evidence"][0]["rationale"] == "The source gives a different count."
    status = store.analysis_status(1)
    assert (status["status"], status["provider"], status["model"], status["prompt_version"]) == (
        "completed", "fake-llm", "fake-model-1", ANALYSIS_PROMPT_VERSION,
    )
    assert status["updated_at"] == "2026-09-28T16:30:00Z"


def test_without_an_assessor_retrieved_pages_stay_pending(tmp_path):
    store = _store(tmp_path, ARTICLES[:1])

    summary = run_pending_analysis(
        store, FakeAnalysis(candidate=True), FakeSearch(("https://register.test/ferry",)), PageFetcher(), 10, now=NOW,
    )

    assert summary == _summary(claims=1, candidates=1, pending=1, analyzed=1)
    assert store.list_findings() == []
    [finding] = store.list_findings(visible_only=False)
    assert finding["evidence"][0]["relation"] == "context" and finding["evidence"][0]["rationale"] is None


def test_assessor_failure_leaves_evidence_unassessed_and_is_ledgered(tmp_path):
    store = _store(tmp_path, ARTICLES[:1])

    summary = run_pending_analysis(
        store, FakeAnalysis(candidate=True), FakeSearch(("https://register.test/ferry",)), PageFetcher(), 10,
        assessor=ContradictsAssessor(fail=True), now=NOW,
    )

    assert summary == _summary(claims=1, candidates=1, pending=1, analyzed=1)
    assert [row[3:6] for row in _ledger(store)] == [("analysis", 1, "ok"), ("assessment", 1, "failed")]


def test_invalid_daily_limit_is_rejected(tmp_path):
    store = _store(tmp_path, ARTICLES[:1])

    for value in (0, -1, True, "5"):
        with pytest.raises(ValueError, match="daily_llm_call_limit"):
            run_pending_analysis(store, FakeAnalysis(), FakeSearch(), NoFetcher(), 10, now=NOW, daily_llm_call_limit=value)


# --- failure isolation, attempts and stale jobs ---------------------------------------------------------

def test_one_failing_revision_does_not_block_the_others(tmp_path):
    store = _store(tmp_path)
    analysis = FakeAnalysis(fail_urls={ARTICLES[1][1]})

    summary = run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW)

    assert summary == _summary(claims=2, analyzed=2, failed=1)
    assert analysis.calls == [1, 2, 3]
    failed = store.analysis_status(2)
    assert (failed["status"], failed["attempts"]) == ("failed", 1)
    assert failed["last_error"].startswith("RuntimeError: provider exploded")
    assert SECRET not in failed["last_error"] and "\n" not in failed["last_error"]
    assert len(failed["last_error"]) <= 500
    assert [store.analysis_status(revision_id)["status"] for revision_id in (1, 3)] == ["completed", "completed"]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims WHERE revision_id = 2").fetchone() == (0,)


def test_failed_revision_is_retried_until_max_attempts(tmp_path):
    store = _store(tmp_path, ARTICLES[:1])
    analysis = FakeAnalysis(fail_urls={ARTICLES[0][1]})

    for attempt in range(1, 4):
        assert run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW) == _summary(failed=1)
        assert store.analysis_status(1)["attempts"] == attempt

    assert run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW) == _summary()
    assert analysis.calls == [1, 1, 1]
    assert run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW, max_attempts=4) == _summary(failed=1)
    assert store.analysis_status(1)["attempts"] == 4


def test_stale_running_job_is_recovered_and_fresh_one_is_left_alone(tmp_path):
    store = _store(tmp_path, ARTICLES[:2])
    store.mark_analysis_running(1, "2026-09-28T14:00:00Z")  # crashed 2.5 hours before NOW
    store.mark_analysis_running(2, "2026-09-28T16:00:00Z")  # another run started 30 minutes ago
    analysis = FakeAnalysis()

    summary = run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW)

    assert summary == _summary(claims=1, analyzed=1)
    assert analysis.calls == [1]
    recovered = store.analysis_status(1)
    assert (recovered["status"], recovered["attempts"]) == ("completed", 1)
    assert store.analysis_status(2)["status"] == "running"


def test_revision_completed_concurrently_is_not_analyzed_again(tmp_path, monkeypatch):
    store = _store(tmp_path, ARTICLES[:1])
    monkeypatch.setattr(store, "mark_analysis_running", lambda revision_id, at: False)
    analysis = FakeAnalysis()

    assert run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW) == _summary()
    assert analysis.calls == []


def test_grouping_error_fails_only_that_revision(tmp_path, monkeypatch):
    store = _store(tmp_path)
    real_group = pipeline.group_revision

    def group(revision, existing):
        if revision.id == 2:
            raise RuntimeError("bad text")
        return real_group(revision, existing)

    monkeypatch.setattr(pipeline, "group_revision", group)

    summary = run_pending_analysis(store, FakeAnalysis(), FakeSearch(), NoFetcher(), 10, now=NOW)

    assert summary == _summary(claims=2, analyzed=2, failed=1)
    assert store.analysis_status(2)["last_error"] == "RuntimeError: bad text"


def test_grouping_is_peer_major_and_pairs_each_revision_with_each_peer_once(tmp_path, monkeypatch):
    store = _store(tmp_path)
    real_group = pipeline.group_revision
    calls = []

    def group(revision, existing):
        calls.append((existing[0].id, revision.id))
        return real_group(revision, existing)

    monkeypatch.setattr(pipeline, "group_revision", group)

    run_pending_analysis(store, FakeAnalysis(), FakeSearch(), NoFetcher(), 10, now=NOW)

    # Peers come newest first (same fetch time, so by id descending); each outer step is one peer.
    assert calls == [(3, 1), (3, 2), (2, 1), (2, 3), (1, 2), (1, 3)]


# --- daily LLM budget -------------------------------------------------------------------------------------

def test_local_day_start_uses_the_moment_offset():
    assert local_day_start(NOW) == LOCAL_MIDNIGHT_UTC
    assert local_day_start(datetime(2026, 9, 28, 23, 59, tzinfo=timezone.utc)) == "2026-09-28T00:00:00Z"


def test_reached_budget_skips_every_listed_revision_without_provider_calls(tmp_path):
    store = _store(tmp_path)
    _record_calls(store, LOCAL_MIDNIGHT_UTC, 2)
    analysis = FakeAnalysis()

    summary = run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW, daily_llm_call_limit=2)

    assert summary == _summary(skipped=3)
    assert analysis.calls == []
    for revision_id in (1, 2, 3):
        status = store.analysis_status(revision_id)
        assert (status["status"], status["attempts"], status["last_error"]) == ("skipped", 0, BUDGET_SKIP_REASON)
    assert len(_ledger(store)) == 2
    # Skipped jobs stay due and run once the budget allows it.
    assert len(store.list_pending_revisions(now=NOW.astimezone(timezone.utc))) == 3


def test_calls_before_local_midnight_do_not_count_towards_today(tmp_path):
    store = _store(tmp_path, ARTICLES[:1])
    # 23:59:59 on 28 September in Taiwan: yesterday, although it is the same UTC date as NOW.
    _record_calls(store, "2026-09-28T15:59:59Z", 5)

    summary = run_pending_analysis(store, FakeAnalysis(), FakeSearch(), NoFetcher(), 10, now=NOW, daily_llm_call_limit=1)

    assert summary == _summary(claims=1, analyzed=1)


def test_budget_counts_calls_made_during_the_run(tmp_path):
    store = _store(tmp_path)
    analysis = FakeAnalysis()

    summary = run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW, daily_llm_call_limit=2)

    assert summary == _summary(claims=2, analyzed=2, skipped=1)
    assert analysis.calls == [1, 2]
    assert store.analysis_status(3)["status"] == "skipped"


def test_budget_reached_before_an_assessment_skips_the_revision_without_an_attempt(tmp_path):
    store = _store(tmp_path, ARTICLES[:2])
    assessor = ContradictsAssessor()
    search = FakeSearch(("https://register.test/one", "https://register.test/two"))

    summary = run_pending_analysis(
        store, FakeAnalysis(candidate=True), search, PageFetcher(), 10, assessor=assessor, now=NOW, daily_llm_call_limit=2,
    )

    assert summary == _summary(skipped=2)
    assert len(assessor.calls) == 1
    assert [row[3:6] for row in _ledger(store)] == [("analysis", 1, "ok"), ("assessment", 1, "ok")]
    status = store.analysis_status(1)
    assert (status["status"], status["attempts"], status["last_error"]) == ("skipped", 0, BUDGET_SKIP_REASON)
    assert store.list_findings(visible_only=False) == []

    tomorrow = NOW + timedelta(days=1)
    summary = run_pending_analysis(
        store, FakeAnalysis(candidate=True), search, PageFetcher(), 10, assessor=assessor, now=tomorrow, daily_llm_call_limit=10,
    )
    assert summary == _summary(claims=2, candidates=2, visible_findings=2, analyzed=2)


# --- LLM ledger ---------------------------------------------------------------------------------------------

def test_every_provider_call_is_ledgered_with_labels_and_sizes(tmp_path):
    store = _store(tmp_path, ARTICLES[:2])

    run_pending_analysis(
        store, FakeAnalysis(candidate=True, fail_urls={ARTICLES[1][1]}), FakeSearch(("https://register.test/ferry",)),
        PageFetcher(), 10, assessor=ContradictsAssessor(), now=NOW,
    )

    assert _ledger(store) == [
        ("2026-09-28T16:30:00Z", "fake-llm", "fake-model-1", "analysis", 1, "ok", 1, 1),
        ("2026-09-28T16:30:00Z", "fake-llm", "fake-assessor-1", "assessment", 1, "ok", 1, 1),
        ("2026-09-28T16:30:00Z", "fake-llm", "fake-model-1", "analysis", 2, "failed", 1, 0),
    ]


def test_null_assessor_spends_no_budget(tmp_path):
    store = _store(tmp_path, ARTICLES[:1])

    summary = run_pending_analysis(
        store, FakeAnalysis(candidate=True), FakeSearch(("https://register.test/ferry",)), PageFetcher(), 10,
        assessor=NullEvidenceAssessor(), now=NOW, daily_llm_call_limit=1,
    )

    assert summary == _summary(claims=1, candidates=1, pending=1, analyzed=1)
    assert [row[3] for row in _ledger(store)] == ["analysis"]


def test_ledger_write_failure_fails_the_revision_instead_of_hiding_the_call(tmp_path, monkeypatch):
    store = _store(tmp_path, ARTICLES[:1])
    real_record = store.record_llm_call

    def record(called_at, provider, model, purpose, *args):
        if purpose == "assessment":
            raise sqlite3.OperationalError("database is locked")
        return real_record(called_at, provider, model, purpose, *args)

    monkeypatch.setattr(store, "record_llm_call", record)

    summary = run_pending_analysis(
        store, FakeAnalysis(candidate=True), FakeSearch(("https://register.test/ferry",)), PageFetcher(), 10,
        assessor=ContradictsAssessor(), now=NOW,
    )

    assert summary == _summary(failed=1)
    assert store.analysis_status(1)["last_error"] == "OperationalError: database is locked"


# --- same-topic context and persistence -------------------------------------------------------------------

def test_high_confidence_peers_are_related_and_grouping_is_persisted(tmp_path):
    store = _pair_store(tmp_path)
    analysis = FakeAnalysis()

    summary = run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW)

    assert summary == _summary(claims=2, candidates=2, pending=2, analyzed=2)
    assert analysis.related == [(2,), (1,)]
    topic_id = store.topic_id_for_revision(1)
    assert topic_id is not None and topic_id == store.topic_id_for_revision(2)
    assert store.topic_status(topic_id) == "active"
    assert [item.id for item in store.list_topic_revisions(topic_id)] == [1, 2]
    [comparison] = store.dashboard_snapshot()["comparisons"]
    assert comparison["confidence"] == "high" and "taipei" in comparison["label"]


def test_possible_peers_are_persisted_but_not_related(tmp_path):
    store = _pair_store(tmp_path, ("https://alpha.test/forum", "https://alpha.test/forum-update"))
    analysis = FakeAnalysis()

    summary = run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW)

    assert summary == _summary(claims=2, analyzed=2)
    assert analysis.related == [(), ()]
    topic_id = store.topic_id_for_revision(1)
    assert store.topic_status(topic_id) == "possible"
    assert [item.id for item in store.list_topic_revisions(topic_id)] == [1, 2]


def test_reviewer_dismissed_pair_is_not_used_as_related_context(tmp_path):
    store = _pair_store(tmp_path)
    topic_id = store.assign_topic(1, 2, "Taipei defense forum", "high")
    assert store.set_topic_status(topic_id, "dismissed")
    analysis = FakeAnalysis()

    summary = run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, assessor=ContradictsAssessor(), now=NOW)

    assert summary == _summary(claims=2, analyzed=2)
    assert analysis.related == [(), ()]
    assert store.topic_status(topic_id) == "dismissed"


def test_superseded_revisions_are_never_peers(tmp_path):
    store = _pair_store(tmp_path)
    # Alpha rewrites its article about something else; its first revision must not stay a peer.
    _save(store, "Alpha", PAIR[0][1], "Taipei defense forum", "Keelung Harbor Bureau renewed twelve ferry permits.")
    analysis = FakeAnalysis()

    assert [item.id for item in store.list_recent_revisions(current_only=True)] == [3, 2]
    assert [item.id for item in store.list_recent_revisions()] == [3, 2, 1]

    run_pending_analysis(store, analysis, FakeSearch(), NoFetcher(), 10, now=NOW)

    assert analysis.calls == [2, 3]
    assert analysis.related == [(), ()]


def test_topic_evidence_is_redacted_peer_text_assessed_by_the_same_assessor(tmp_path):
    urls = ("https://alpha.test/forum?session=alpha-secret", "https://bravo.test/forum?token=bravo-secret")
    store = _pair_store(tmp_path, urls)
    assessor = ContradictsAssessor()

    summary = run_pending_analysis(store, FakeAnalysis(), FakeSearch(), NoFetcher(), 10, assessor=assessor, now=NOW)

    # A credential in a peer URL is redacted on both sides, so the assessed difference still counts.
    assert summary == _summary(claims=2, candidates=2, visible_findings=2, analyzed=2)
    assert [(call[1], call[2]) for call in assessor.calls] == [(1, "related_article"), (2, "related_article")]
    assert assessor.calls[0][0] == "100 troops"
    findings = store.list_findings(visible_only=False)
    for finding in findings:
        [evidence] = finding["evidence"]
        assert (evidence["source_kind"], evidence["provider"], evidence["status"]) == (
            "related_article", "related_revision", "retrieved",
        )
        assert "alpha-secret" not in str(finding) and "bravo-secret" not in str(finding)


def test_topic_status_reads_a_topic_or_none(tmp_path):
    store = _pair_store(tmp_path)
    topic_id = store.assign_topic(1, 2, "Taipei defense forum", "possible")

    assert store.topic_status(topic_id) == "possible"
    assert store.topic_status(topic_id + 1) is None
