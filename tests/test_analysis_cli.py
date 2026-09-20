from pathlib import Path
import sqlite3

from gaohe.cli import main, run_pending_analysis
import pytest

from gaohe.domain import ArticleCandidate, Claim, Evidence, FetchedArticle, Finding, SearchHit, Source, article_content_hash
from gaohe.providers import AnalysisResult, FindingCandidate
from gaohe.storage import Store


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


def test_runner_persists_ordinary_claims_without_visible_findings_and_hides_article_text(tmp_path):
    store = _store_with_revisions(tmp_path)
    analysis = FakeAnalysis()

    summary = run_pending_analysis(store, analysis, FakeSearch(), EmptyFetcher(), 10)

    assert summary == {"claims": 1, "candidates": 0, "visible_findings": 0, "pending": 0, "retrieval_failures": 0}
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

    assert summary == {"claims": 1, "candidates": 1, "visible_findings": 0, "pending": 1, "retrieval_failures": 1}
    assert store.list_pending_revisions() == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT claim_id FROM findings").fetchone()[0] == 1
        assert connection.execute("SELECT finding_id, status FROM evidence").fetchone() == (1, "retrieval_failed")


def test_runner_passes_only_high_confidence_peer_context(tmp_path):
    store = _store_with_revisions(tmp_path, 2)
    analysis = FakeAnalysis()

    run_pending_analysis(store, analysis, FakeSearch(), EmptyFetcher(), 10)

    assert [len(items) for items in analysis.related] == [1, 1]


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
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=gemini\nLLM_MODEL=test\nLLM_API_KEY=super-secret\n", encoding="utf-8")

    assert main(["analyze", "--pending", "--env-file", str(env_file)]) == 0
    output = capsys.readouterr().out
    assert output == "claims=1 candidates=0 visible_findings=0 pending=0 retrieval_failures=0\n"
    assert "Taipei Ministry" not in output
    assert "super-secret" not in output


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
