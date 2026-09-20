from dataclasses import dataclass

from gaohe.domain import ArticleRevision, Claim, Evidence, RetrievedPage, SearchHit, article_content_hash
from gaohe.providers import AnalysisResult, FindingCandidate


def revision(text="The report says 100 units.", revision_id=7):
    return ArticleRevision(revision_id, 3, "https://news.test/article", "Article", text, article_content_hash("Article", text), "2026-09-20T00:00:00Z")


def candidate(item, finding_type="factual_contradiction", query="100 units official record"):
    text = "100 units"
    start = item.text.index(text)
    return FindingCandidate(None, finding_type, "Check the reported figure", start, start + len(text), "material", query)


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
        (first, "retrieved", "official", "2026-09-19T00:00:00Z"),
        (second, "insufficient_scope", "other", None),
    ]
    assert result[0].excerpt == "Official record: 200 units."
    assert result[1].excerpt == ""


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
    assert resolve_finding(cross_media, [evidence(source_kind="related_article")], (related,)).visible is True
    inference = candidate(item, "unsupported_inference")
    assert resolve_finding(inference, [evidence(relation="supports")], ()).visible is False
    assert resolve_finding(inference, [evidence(relation="context")], ()).visible is True


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

    def analyze(self, item, related):
        self.calls += 1
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
