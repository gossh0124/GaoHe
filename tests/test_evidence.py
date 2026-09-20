from dataclasses import dataclass

from gaohe.domain import ArticleRevision, Claim, Evidence, RetrievedPage, SearchHit, article_content_hash
from gaohe.providers import AnalysisResult, FindingCandidate


def revision(text="The report says 100 units.", revision_id=7):
    return ArticleRevision(revision_id, 3, "https://news.test/article", "Article", text, article_content_hash("Article", text), "2026-09-20T00:00:00Z")


def candidate(item, finding_type="factual_contradiction", query="100 units official record"):
    text = "100 units"
    start = item.text.index(text)
    return FindingCandidate(None, finding_type, "Check the reported figure", start, start + len(text), "material", query, item.id)


@dataclass
class Search:
    hits: tuple[SearchHit, ...]
    calls: list[tuple[str, int]]

    def search(self, query, limit=5):
        self.calls.append((query, limit))
        return self.hits


@dataclass
class Fetcher:
    pages: dict[str, RetrievedPage]
    calls: list[str]

    def fetch(self, url):
        self.calls.append(url)
        return self.pages[url]


def page(url, text="Official record: 200 units.", status="retrieved"):
    return RetrievedPage(url, "Official record", text, "2026-09-20T01:00:00Z", status, article_content_hash("Official record", text) if text else None)


def evidence(status="retrieved", relation="contradicts", source_kind="search"):
    return Evidence(None, None, "https://record.test/a", "Record", "200 units", relation, status, source_kind, "2026-09-20T01:00:00Z", "official", "2026-09-19T00:00:00Z", "hash")


def test_retrieve_evidence_bounds_query_deduplicates_urls_and_requires_full_text():
    from gaohe.analysis import retrieve_evidence
    from gaohe.providers import MAX_QUERY_CHARS, MAX_SEARCH_LIMIT

    item = revision()
    proposed = candidate(item, query="x" * (MAX_QUERY_CHARS + 10))
    first = "https://record.test/a?edition=1#section"
    duplicate = "https://record.test/a?edition=1#other"
    second = "https://record.test/b"
    search = Search((SearchHit(first, "First", "snippet", "official", "2026-09-19T00:00:00Z"), SearchHit(duplicate, "Duplicate", "snippet", "official", None), SearchHit(second, "Second", "snippet", "other", None)), [])
    fetcher = Fetcher({first: page(first), second: page(second, "", "parse_error")}, [])

    result = retrieve_evidence(proposed, search, fetcher, limit=MAX_SEARCH_LIMIT + 5)

    assert search.calls == [("x" * MAX_QUERY_CHARS, MAX_SEARCH_LIMIT)]
    assert fetcher.calls == [first, second]
    assert [(item.url, item.status, item.provider, item.published_at) for item in result] == [
        (first.removesuffix("#section"), "retrieved", "official", "2026-09-19T00:00:00Z"),
        (second, "insufficient_scope", "other", None),
    ]
    assert result[0].excerpt == "Official record: 200 units."
    assert result[1].excerpt == ""
    assert result[0].relation == "context"


def test_retrieve_evidence_maps_timeout_http_and_firecrawl_results_without_snippet_evidence():
    from gaohe.analysis import retrieve_evidence

    item = revision()
    proposed = candidate(item)
    timeout = "https://record.test/timeout"
    http = "https://record.test/http"
    fallback = "https://record.test/fallback"
    search = Search(tuple(SearchHit(url, "Title", "discovery only", "search", None) for url in (timeout, http, fallback)), [])
    fetcher = Fetcher({timeout: page(timeout, "", "timeout"), http: page(http, "", "http_error"), fallback: page(fallback, "Fallback text")}, [])

    result = retrieve_evidence(proposed, search, fetcher)

    assert [item.status for item in result] == ["retrieval_failed", "retrieval_failed", "retrieved"]
    assert [item.excerpt for item in result] == ["", "", "Fallback text"]
    assert all("discovery only" not in item.excerpt for item in result)


def test_resolve_finding_is_visible_only_for_required_full_text_relations():
    from gaohe.analysis import resolve_finding

    item = revision()
    proposed = candidate(item)

    visible = resolve_finding(proposed, [evidence()], ())
    assert visible.visible is True
    assert visible.status == "resolved"
    assert resolve_finding(proposed, [evidence(relation="supports")], ()).visible is False
    assert resolve_finding(proposed, [evidence(status="retrieval_failed")], ()).visible is False
    assert resolve_finding(proposed, [], ()).evidence_status == "pending"


def test_cross_media_and_inference_require_their_specific_evidence():
    from gaohe.analysis import resolve_finding

    item = revision()
    cross_media = candidate(item, "material_cross_media_difference")
    related = revision("Related article", revision_id=8)
    assert resolve_finding(cross_media, [evidence(source_kind="related_article")], ()).visible is False
    linked = Evidence(None, None, related.url, "Record", "200 units", "contradicts", "retrieved", "related_article", "2026-09-20T01:00:00Z", "official", None, "hash")
    assert resolve_finding(cross_media, [linked], (related,)).visible is True
    inference = candidate(item, "unsupported_inference")
    assert resolve_finding(inference, [evidence(relation="supports")], ()).visible is False
    assert resolve_finding(inference, [evidence(relation="context", source_kind="direct")], ()).visible is True


def test_retrieval_alone_never_supplies_the_explicit_limiting_relation():
    from gaohe.analysis import retrieve_evidence, resolve_finding

    item = revision()
    proposed = candidate(item, "unsupported_inference")
    url = "https://record.test/limit"
    search = Search((SearchHit(url, "Record", "snippet", "official", None),), [])
    fetched = retrieve_evidence(proposed, search, Fetcher({url: page(url)}, []))

    assert resolve_finding(proposed, fetched, ()).visible is False


@dataclass
class Analysis:
    result: AnalysisResult
    calls: int = 0
    related: tuple = ()

    def analyze(self, item, related):
        self.calls += 1
        self.related = tuple(related)
        return self.result


def test_analyze_revision_returns_complete_batch_and_skips_evidence_when_no_candidates():
    from gaohe.analysis import analyze_revision

    item = revision()
    stated = Claim(None, item.id, "100 units", 16, 25, "checkable", "ordinary", "extracted")
    analysis = Analysis(AnalysisResult(item.id, (stated,), ()))
    search = Search((), [])
    fetcher = Fetcher({}, [])

    result = analyze_revision(item, (), analysis, search, fetcher)

    assert analysis.calls == 1
    assert result.claims == (stated,)
    assert result.candidates == ()
    assert result.evidence == ()
    assert result.findings == ()
    assert search.calls == []
    assert fetcher.calls == []


def test_retrieve_evidence_rejects_secret_queries_credentials_and_fetch_failures():
    from gaohe.analysis import retrieve_evidence

    item = revision()
    search = Search((SearchHit("https://record.test/a", "Title", "snippet", "official", None),), [])
    assert retrieve_evidence(candidate(item, query="api_key=secret"), search, Fetcher({}, [])) == []
    assert retrieve_evidence(candidate(item, query="https://user:secret@record.test/a"), search, Fetcher({}, [])) == []
    failed = retrieve_evidence(candidate(item), search, Fetcher({}, []))
    assert failed[0].status == "retrieval_failed"
    assert failed[0].excerpt == ""


def test_retrieve_evidence_redacts_bounds_text_and_rejects_credential_urls():
    from gaohe.analysis import retrieve_evidence
    from gaohe.storage import MAX_EVIDENCE_EXCERPT_CHARS

    item = revision()
    safe = "https://record.test/a#fragment"
    secret = "https://user:secret@record.test/private"
    text = "Bearer secret-token " + "x" * (MAX_EVIDENCE_EXCERPT_CHARS + 10)
    search = Search((SearchHit(secret, "Secret", "snippet", "official", None), SearchHit(safe, "Safe", "snippet", "official", None)), [])
    result = retrieve_evidence(candidate(item), search, Fetcher({safe: page(safe, text)}, []))
    assert len(result) == 1
    assert result[0].url == "https://record.test/a"
    assert "secret-token" not in result[0].excerpt
    assert len(result[0].excerpt) <= MAX_EVIDENCE_EXCERPT_CHARS


def test_resolve_finding_rejects_missing_identity():
    from gaohe.analysis import resolve_finding
    import pytest

    with pytest.raises(ValueError, match="revision_id"):
        item = revision()
        resolve_finding(FindingCandidate(None, "factual_contradiction", "Check", 16, 25, "material", None), [], ())
    item = revision()
    with pytest.raises(ValueError, match="does not match"):
        resolve_finding(FindingCandidate(None, "factual_contradiction", "Check", 16, 25, "material", None, 8), [], (), item)


def test_analyze_revision_passes_related_and_rejects_cross_revision_candidates():
    from gaohe.analysis import analyze_revision
    import pytest

    item = revision()
    related = revision("Related article", revision_id=8)
    stated = Claim(None, item.id, "100 units", 16, 25, "checkable", "material", "extracted")
    proposed = FindingCandidate(None, "factual_contradiction", "Check", 16, 25, "material", None, 8)
    analysis = Analysis(AnalysisResult(item.id, (stated,), (proposed,)))
    with pytest.raises(ValueError, match="candidate revision_id"):
        analyze_revision(item, (related,), analysis, Search((), []), Fetcher({}, []))
    assert analysis.related == (related,)


def test_analyze_revision_never_searches_a_candidate_query_that_is_the_full_article():
    from gaohe.analysis import analyze_revision

    item = revision("The report says 100 units.")
    stated = Claim(None, item.id, "100 units", 16, 25, "checkable", "material", "extracted")
    proposed = FindingCandidate(None, "factual_contradiction", "Check", 16, 25, "material", item.text, item.id)
    search = Search((), [])
    result = analyze_revision(item, (), Analysis(AnalysisResult(item.id, (stated,), (proposed,))), search, Fetcher({}, []))
    assert result.evidence == ()
    assert search.calls == []
