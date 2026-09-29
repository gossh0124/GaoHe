from pathlib import Path
import sqlite3

from gaohe.cli import main, run_pending_analysis
import pytest

from gaohe.domain import (
    ArticleCandidate,
    Claim,
    Evidence,
    EvidenceAssessment,
    FetchedArticle,
    Finding,
    SearchHit,
    Source,
    article_content_hash,
)
from gaohe.pipeline import SUMMARY_KEYS
from gaohe.providers import AnalysisResult, FindingCandidate
from gaohe.storage import Store


def _summary(**counts: int) -> dict[str, int]:
    return {key: counts.get(key, 0) for key in SUMMARY_KEYS}


class FakeAnalysis:
    def __init__(self, *, candidate: bool = False) -> None:
        self.related = []
        self.candidate = candidate

    def analyze(self, revision, related):
        self.related.append(tuple(related))
        text = "reported figure"
        start = revision.text.index(text)
        claim = Claim(None, revision.id, text, start, start + len(text), "checkable", "material", "extracted")
        candidates = ()
        if self.candidate:
            candidates = (FindingCandidate(None, "factual_contradiction", "Check the figure", start, start + len(text), "material", "figure source"),)
        return AnalysisResult(revision.id, (claim,), candidates)


class TwoCandidateAnalysis:
    def analyze(self, revision, related):
        first = "reported figure"
        second = "reported source"
        claims = tuple(
            Claim(None, revision.id, text, revision.text.index(text), revision.text.index(text) + len(text), "checkable", "material", "extracted")
            for text in (first, second)
        )
        candidates = tuple(
            FindingCandidate(None, "factual_contradiction", f"Check {claim.text}", claim.start, claim.end, "material", f"independent {index} record")
            for index, claim in enumerate(claims)
        )
        return AnalysisResult(revision.id, claims, candidates)


class FakeSearch:
    def __init__(self, hits=()):
        self.hits = hits

    def search(self, query, limit=5):
        return self.hits


class FailingFetcher:
    def fetch(self, url):
        raise RuntimeError("secret should not escape")


class EmptyFetcher:
    def fetch(self, url):
        raise AssertionError("no evidence should be fetched")


class PeerContradictsAssessor:
    """Reads a same-topic peer as contradicting the claim, quoting the peer verbatim."""

    provider_name = "fake"
    model = "fake-assessor"

    def __init__(self) -> None:
        self.calls = []

    def assess(self, claim_text, revision, evidence, finding_type):
        self.calls.append((claim_text, revision.id, evidence.source_kind, finding_type))
        quote = evidence.excerpt.split(";")[0]
        return EvidenceAssessment("contradicts", "The peer article reports a different troop count.", quote)


def _store_with_revisions(tmp_path: Path, count: int = 1) -> Store:
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    for index in range(count):
        source_id = store.add_source(Source(None, f"Source {index}", f"https://source-{index}.test/feed"))
        candidate = ArticleCandidate(
            source_id,
            f"https://source-{index}.test/article",
            "Taipei coastal permit update",
            None,
            "2026-09-20T00:00:00Z",
            {},
        )
        text = f"Taipei Ministry deployed coastal permit reported figure {index}."
        store.save_fetched_article(FetchedArticle(candidate, text, "2026-09-20T00:00:00Z", article_content_hash(candidate.title, text)))
    return store


def _store_with_topic_pair(tmp_path: Path) -> Store:
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    for name, url, text in (
        ("Alpha", "https://alpha.test/forum", "Taipei Defense Ministry forum opens with 100 troops; reported figure."),
        ("Bravo", "https://bravo.test/forum", "Taipei Defense Ministry forum opens with 1000 troops; reported figure."),
    ):
        source_id = store.add_source(Source(None, name, f"{url}/feed"))
        candidate = ArticleCandidate(source_id, url, "Taipei defense forum", None, "2026-09-20T00:00:00Z", {})
        store.save_fetched_article(FetchedArticle(candidate, text, "2026-09-20T00:00:00Z", article_content_hash(candidate.title, text)))
    return store


def test_runner_persists_ordinary_claims_without_visible_findings_and_hides_article_text(tmp_path):
    store = _store_with_revisions(tmp_path)
    analysis = FakeAnalysis()

    summary = run_pending_analysis(store, analysis, FakeSearch(), EmptyFetcher(), 10)

    assert summary == _summary(claims=1, analyzed=1)
    assert store.list_pending_revisions() == []
    assert "Taipei Ministry" not in str(summary)


def test_runner_counts_retrieval_failure_but_completes_revision(tmp_path):
    store = _store_with_revisions(tmp_path)

    summary = run_pending_analysis(
        store,
        FakeAnalysis(candidate=True),
        FakeSearch((SearchHit("https://evidence.test/record", "Record", "snippet", "official", None),)),
        FailingFetcher(),
        10,
    )

    assert summary == _summary(claims=1, candidates=1, pending=1, retrieval_failures=1, analyzed=1)
    assert store.list_pending_revisions() == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT claim_id FROM findings").fetchone()[0] == 1
        assert connection.execute("SELECT finding_id, status FROM evidence").fetchone() == (1, "retrieval_failed")


def test_runner_passes_only_high_confidence_peer_context(tmp_path):
    store = _store_with_revisions(tmp_path, 2)
    analysis = FakeAnalysis()

    run_pending_analysis(store, analysis, FakeSearch(), EmptyFetcher(), 10)

    assert [len(items) for items in analysis.related] == [1, 1]


def test_runner_persists_topic_candidate_for_later_pending_revision(tmp_path):
    store = _store_with_topic_pair(tmp_path)
    first = store.list_pending_revisions(1)[0]
    analysis = FakeAnalysis()
    assessor = PeerContradictsAssessor()

    assert run_pending_analysis(store, analysis, FakeSearch(), EmptyFetcher(), 1, assessor=assessor)["candidates"] == 1
    assert store.list_pending_revisions() == [item for item in store.list_recent_revisions() if item.id != first.id]

    summary = run_pending_analysis(store, analysis, FakeSearch(), EmptyFetcher(), 1, assessor=assessor)

    assert summary == _summary(claims=1, candidates=1, visible_findings=1, analyzed=1)
    assert len(analysis.related[-1]) == 1
    assert [call[2:] for call in assessor.calls] == [("related_article", "material_cross_media_difference")] * 2
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT finding_type, visible FROM findings ORDER BY id").fetchall() == [
            ("material_cross_media_difference", 1),
            ("material_cross_media_difference", 1),
        ]
        rows = connection.execute("SELECT relation, status, source_kind, excerpt, rationale FROM evidence ORDER BY id").fetchall()
    assert [row[:3] for row in rows] == [
        ("contradicts", "retrieved", "related_article"),
        ("contradicts", "retrieved", "related_article"),
    ]
    assert all(len(excerpt) <= 2_000 for _, _, _, excerpt, _ in rows)
    assert all(rationale == "The peer article reports a different troop count." for *_, rationale in rows)


def test_runner_keeps_topic_candidate_pending_without_an_assessor(tmp_path):
    store = _store_with_topic_pair(tmp_path)

    summary = run_pending_analysis(store, FakeAnalysis(), FakeSearch(), EmptyFetcher(), 10)

    assert summary == _summary(claims=2, candidates=2, pending=2, analyzed=2)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT finding_type, status, visible FROM findings ORDER BY id").fetchall() == [
            ("material_cross_media_difference", "pending", 0),
            ("material_cross_media_difference", "pending", 0),
        ]
        rows = connection.execute("SELECT relation, status, source_kind, rationale FROM evidence ORDER BY id").fetchall()
    assert rows == [("context", "retrieved", "related_article", None)] * 2
    assert store.list_findings() == []


def test_analyze_cli_rejects_unwired_firecrawl_configuration(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=gemini\nLLM_MODEL=test\nLLM_API_KEY=key\nWEB_SEARCH_PROVIDER=firecrawl\nFIRECRAWL_API_KEY=key\n", encoding="utf-8")

    assert main(["analyze", "--pending", "--env-file", str(env_file)]) == 2
    assert capsys.readouterr().err == "error: analyze unavailable\n"


def test_analyze_cli_rejects_invalid_limit_and_missing_configuration(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATA_DIR={tmp_path}\n", encoding="utf-8")

    assert main(["analyze", "--pending", "--limit", "0", "--env-file", str(env_file)]) == 2
    assert "error:" in capsys.readouterr().err
    assert main(["analyze", "--pending", "--env-file", str(env_file)]) == 2
    assert "error:" in capsys.readouterr().err


def test_analyze_cli_prints_only_counts_and_hides_secret_article_text(tmp_path, capsys, monkeypatch):
    import gaohe.cli as cli

    store = _store_with_revisions(tmp_path)
    monkeypatch.setattr(cli, "_store", lambda settings: store)
    monkeypatch.setattr(cli, "build_analysis_provider", lambda settings: FakeAnalysis())
    monkeypatch.setattr(cli, "build_search_provider", lambda settings: FakeSearch())
    monkeypatch.setattr(cli, "build_evidence_assessor", lambda settings: PeerContradictsAssessor())
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=gemini\nLLM_MODEL=test\nLLM_API_KEY=super-secret\n", encoding="utf-8")

    assert main(["analyze", "--pending", "--env-file", str(env_file)]) == 0
    output = capsys.readouterr().out
    assert output == (
        "claims=1 candidates=0 visible_findings=0 pending=0 retrieval_failures=0 "
        "rejected_claims=0 analyzed=1 failed=0 skipped=0\n"
    )
    assert "Taipei Ministry" not in output
    assert "super-secret" not in output


def test_analyze_cli_wires_the_evidence_assessor_and_daily_call_limit(tmp_path, capsys, monkeypatch):
    import gaohe.cli as cli

    store = _store_with_topic_pair(tmp_path)
    assessor = PeerContradictsAssessor()
    monkeypatch.setattr(cli, "_store", lambda settings: store)
    monkeypatch.setattr(cli, "build_analysis_provider", lambda settings: FakeAnalysis())
    monkeypatch.setattr(cli, "build_search_provider", lambda settings: FakeSearch())
    monkeypatch.setattr(cli, "build_evidence_assessor", lambda settings: assessor)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_PROVIDER=gemini\nLLM_MODEL=test\nLLM_API_KEY=super-secret\nDAILY_LLM_CALL_LIMIT=3\n", encoding="utf-8"
    )

    assert main(["analyze", "--pending", "--env-file", str(env_file)]) == 0
    # The first revision spends two calls; the second spends the last one on analysis and is
    # deferred before its assessment.
    assert capsys.readouterr().out == (
        "claims=1 candidates=1 visible_findings=1 pending=0 retrieval_failures=0 "
        "rejected_claims=0 analyzed=1 failed=0 skipped=1\n"
    )
    assert len(assessor.calls) == 1
    assert store.analysis_status(2)["status"] == "skipped"


def test_analyze_cli_returns_two_for_storage_error(tmp_path, capsys, monkeypatch):
    import gaohe.cli as cli

    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=gemini\nLLM_MODEL=test\nLLM_API_KEY=super-secret\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_store", lambda settings: (_ for _ in ()).throw(sqlite3.Error("secret")))

    assert main(["analyze", "--pending", "--env-file", str(env_file)]) == 2
    assert capsys.readouterr().err == "error: analyze unavailable\n"


def test_analysis_storage_rolls_back_claims_findings_and_completion_together(tmp_path):
    store = _store_with_revisions(tmp_path)
    revision = store.list_pending_revisions()[0]
    start = revision.text.index("reported figure")
    claim = Claim(None, revision.id, "reported figure", start, start + len("reported figure"), "checkable", "material", "extracted")
    finding = Finding(None, revision.id, None, "factual_contradiction", "Check", start, start + len("reported figure"), "pending", "pending", False)
    invalid = Evidence(None, None, "https://evidence.test/record", "Record", "", "context", "pending", "invalid", None)

    with pytest.raises(ValueError, match="source_kind"):
        store.save_analysis(revision.id, (claim,), (finding,), ((invalid,),))

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM findings").fetchone() == (0,)
    assert [item.id for item in store.list_pending_revisions()] == [revision.id]


def test_runner_keeps_each_evidence_batch_linked_to_its_finding(tmp_path):
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Source", "https://source.test/feed"))
    candidate = ArticleCandidate(source_id, "https://source.test/article", "Taipei coastal permit update", None, "2026-09-20T00:00:00Z", {})
    text = "Taipei Ministry deployed coastal permit reported figure and reported source."
    store.save_fetched_article(FetchedArticle(candidate, text, "2026-09-20T00:00:00Z", article_content_hash(candidate.title, text)))

    run_pending_analysis(
        store,
        TwoCandidateAnalysis(),
        FakeSearch((SearchHit("https://evidence.test/record", "Record", "", "official", None),)),
        FailingFetcher(),
        10,
    )

    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT finding_id FROM evidence ORDER BY id").fetchall()
    assert rows == [(1,), (2,)]
