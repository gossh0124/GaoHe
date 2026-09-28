from dataclasses import dataclass, field, replace

from gaohe.domain import ArticleRevision, Claim, Evidence, EvidenceAssessment, RetrievedPage, SearchHit, article_content_hash
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


def page(url, text="Official record: 200 units.", status="retrieved", fetched_via="direct"):
    content_hash = article_content_hash("Official record", text) if text else None
    return RetrievedPage(url, "Official record", text, "2026-09-20T01:00:00Z", status, content_hash, fetched_via)


def evidence(status="retrieved", relation="contradicts", source_kind="direct", rationale="The record says 200 units."):
    return Evidence(
        None, None, "https://record.test/a", "Record", "200 units", relation, status, source_kind,
        "2026-09-20T01:00:00Z", "official", "2026-09-19T00:00:00Z", "hash", rationale,
    )


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
    assert [(item.source_kind, item.rationale) for item in result] == [("direct", None), ("search", None)]


def test_retrieve_evidence_maps_timeout_http_and_firecrawl_results_without_snippet_evidence():
    from gaohe.analysis import retrieve_evidence

    item = revision()
    proposed = candidate(item)
    timeout = "https://record.test/timeout"
    http = "https://record.test/http"
    fallback = "https://record.test/fallback"
    search = Search(tuple(SearchHit(url, "Title", "discovery only", "search", None) for url in (timeout, http, fallback)), [])
    pages = {
        timeout: page(timeout, "", "timeout"),
        http: page(http, "", "http_error"),
        fallback: page(fallback, "Fallback text", fetched_via="firecrawl"),
    }
    fetcher = Fetcher(pages, [])

    result = retrieve_evidence(proposed, search, fetcher)

    assert [item.status for item in result] == ["retrieval_failed", "retrieval_failed", "retrieved"]
    assert [item.excerpt for item in result] == ["", "", "Fallback text"]
    assert [item.source_kind for item in result] == ["search", "search", "firecrawl"]
    assert [item.provider for item in result] == ["search", "search", "search"]
    assert all("discovery only" not in item.excerpt for item in result)


def test_resolve_finding_is_visible_only_for_required_full_text_relations():
    from gaohe.analysis import resolve_finding

    item = revision()
    proposed = candidate(item)

    visible = resolve_finding(proposed, [evidence()], ())
    assert visible.visible is True
    assert visible.status == "resolved"
    assert resolve_finding(proposed, [evidence(source_kind="firecrawl")], ()).visible is True
    assert resolve_finding(proposed, [evidence(relation="supports")], ()).visible is False
    assert resolve_finding(proposed, [evidence(status="retrieval_failed")], ()).visible is False
    # A search lead or an unassessed page never carries a contradiction.
    assert resolve_finding(proposed, [evidence(source_kind="search")], ()).visible is False
    assert resolve_finding(proposed, [evidence(rationale=None)], ()).visible is False
    assert resolve_finding(proposed, [], ()).evidence_status == "pending"


def test_cross_media_and_inference_require_their_specific_evidence():
    from gaohe.analysis import resolve_finding

    item = revision()
    cross_media = candidate(item, "material_cross_media_difference")
    related = revision("Related article", revision_id=8)
    assert resolve_finding(cross_media, [evidence(source_kind="related_article")], ()).visible is False
    linked = Evidence(
        None, None, related.url, "Record", "200 units", "contradicts", "retrieved", "related_article",
        "2026-09-20T01:00:00Z", "official", None, "hash", "The peer reports 200 units.",
    )
    assert resolve_finding(cross_media, [linked], (related,)).visible is True
    assert resolve_finding(cross_media, [replace(linked, rationale=None)], (related,)).visible is False
    inference = candidate(item, "unsupported_inference")
    assert resolve_finding(inference, [evidence(relation="supports")], ()).visible is False
    assert resolve_finding(inference, [evidence(relation="context", source_kind="direct")], ()).visible is True
    assert resolve_finding(inference, [evidence(relation="context", source_kind="search")], ()).visible is False


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


def test_analyze_revision_never_searches_a_prefixed_full_article_query():
    from gaohe.analysis import analyze_revision

    item = revision("The report says 100 units.")
    stated = Claim(None, item.id, "100 units", 16, 25, "checkable", "material", "extracted")
    proposed = FindingCandidate(None, "factual_contradiction", "Check", 16, 25, "material", f"Check: {item.text}", item.id)
    search = Search((), [])

    result = analyze_revision(item, (), Analysis(AnalysisResult(item.id, (stated,), (proposed,))), search, Fetcher({}, []))

    assert result.evidence == ()
    assert search.calls == []


def test_analyze_revision_never_searches_a_suffixed_full_article_query():
    from gaohe.analysis import analyze_revision

    item = revision("The report says 100 units.")
    stated = Claim(None, item.id, "100 units", 16, 25, "checkable", "material", "extracted")
    proposed = FindingCandidate(None, "factual_contradiction", "Check", 16, 25, "material", f"{item.text} for verification", item.id)
    search = Search((), [])

    result = analyze_revision(item, (), Analysis(AnalysisResult(item.id, (stated,), (proposed,))), search, Fetcher({}, []))

    assert result.evidence == ()
    assert search.calls == []


def test_retrieve_evidence_skips_malformed_hit_and_keeps_other_hits():
    from gaohe.analysis import retrieve_evidence

    item = revision()
    safe = "https://record.test/safe"
    search = Search((SearchHit("https://[bad", "Bad", "", "official", None), SearchHit(safe, "Safe", "", "official", None)), [])

    result = retrieve_evidence(candidate(item), search, Fetcher({safe: page(safe)}, []))

    assert [(item.url, item.status) for item in result] == [(safe, "retrieved")]


def test_retrieve_evidence_records_each_fetch_failure_against_its_own_hit():
    from gaohe.analysis import retrieve_evidence

    item = revision()
    failed = "https://record.test/failed"
    safe = "https://record.test/safe"
    search = Search((SearchHit(failed, "Failed", "", "official", None), SearchHit(safe, "Safe", "", "official", None)), [])

    result = retrieve_evidence(candidate(item), search, Fetcher({safe: page(safe)}, []))

    assert [(item.url, item.status) for item in result] == [(failed, "retrieval_failed"), (safe, "retrieved")]


def test_retrieve_evidence_does_not_trust_an_unknown_fetch_path_as_full_text():
    from gaohe.analysis import retrieve_evidence

    item = revision()
    url = "https://record.test/cache"
    search = Search((SearchHit(url, "Cached", "", "official", None),), [])

    (result,) = retrieve_evidence(candidate(item), search, Fetcher({url: page(url, fetched_via="cache")}, []))

    assert (result.status, result.source_kind, result.provider) == ("retrieved", "search", "official")


# --- end to end: extract -> retrieve -> assess -> resolve -----------------------------------------------


ARTICLE = "市府表示補助 1,000 萬元。市長稱此舉必將帶動觀光。報導指出共 30 所學校受惠。"
OFFICIAL = "https://gov.test/budget"
LEAD = "https://blog.test/schools"
SUPPORT = "https://other.test/budget"


def e2e_revision():
    return revision(ARTICLE, revision_id=21)


def e2e_analysis(item):
    def claim(text, kind="checkable", materiality="material"):
        start = item.text.index(text)
        return Claim(None, item.id, text, start, start + len(text), kind, materiality, "extracted")

    def check(stated, summary, query):
        return FindingCandidate(None, "factual_contradiction", summary, stated.start, stated.end, "material", query)

    subsidy = claim("市府表示補助 1,000 萬元", "attributed_statement")
    ordinary = claim("市長稱此舉必將帶動觀光", "opinion", "ordinary")
    schools = claim("報導指出共 30 所學校受惠")
    bad = Claim(None, item.id, "invented", 0, 3, "checkable", "material", "extracted")
    candidates = (check(subsidy, "核對補助金額", "市府 補助 預算"), check(schools, "核對受惠學校數", "受惠 學校 名單"))
    return Analysis(AnalysisResult(item.id, (subsidy, ordinary, schools, bad), candidates, rejected_claims=1))


@dataclass
class RoutedSearch:
    routes: dict[str, tuple[SearchHit, ...]]
    calls: list[str] = field(default_factory=list)

    def search(self, query, limit=5):
        self.calls.append(query)
        return self.routes.get(query, ())


class LookupFetcher:
    """Fetches from a fixed page table; any other URL fails like a blocked page."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError("403 forbidden")
        return self.pages[url]


@dataclass
class ScriptedAssessor:
    answers: dict[str, EvidenceAssessment]
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def assess(self, claim_text, item, record, finding_type):
        self.calls.append((claim_text, record.url, finding_type))
        return self.answers.get(record.url)


def e2e_search(*budget_urls):
    return RoutedSearch({
        "市府 補助 預算": tuple(SearchHit(url, "Budget", "補助 100 萬元", "gov", None) for url in budget_urls),
        # The snippet itself "contradicts", but a snippet is only a lead.
        "受惠 學校 名單": (SearchHit(LEAD, "Schools", "僅 3 所學校受惠，與報導的 30 所不同", "blog", None),),
    })


def e2e_pages():
    return {
        OFFICIAL: page(OFFICIAL, "預算書：本案補助 100 萬元。"),
        SUPPORT: page(SUPPORT, "另一份文件：補助 1,000 萬元。", fetched_via="firecrawl"),
    }


CONTRADICTS = EvidenceAssessment("contradicts", "預算書記載補助 100 萬元，而非 1,000 萬元。", "本案補助 100 萬元")


def test_analyze_revision_makes_exactly_one_contradiction_visible_and_keeps_a_snippet_only_candidate_pending():
    from gaohe.analysis import analyze_revision

    item = e2e_revision()
    search = e2e_search(OFFICIAL)
    fetcher = LookupFetcher(e2e_pages())
    assessor = ScriptedAssessor({OFFICIAL: CONTRADICTS, LEAD: CONTRADICTS})

    result = analyze_revision(item, (), e2e_analysis(item), search, fetcher, assessor)

    assert [claim.text for claim in result.claims] == [
        "市府表示補助 1,000 萬元", "市長稱此舉必將帶動觀光", "報導指出共 30 所學校受惠",
    ]
    assert result.rejected_claims == 2
    assert [finding.visible for finding in result.findings] == [True, False]
    visible, pending = result.findings
    assert (visible.finding_type, visible.status, visible.evidence_status) == ("factual_contradiction", "resolved", "retrieved")
    assert item.text[visible.start:visible.end] == "市府表示補助 1,000 萬元"
    assert (pending.status, pending.evidence_status) == ("pending", "retrieval_failed")
    assert all(finding.revision_id == item.id for finding in result.findings)
    official, lead = result.evidence
    assert (official.relation, official.source_kind, official.provider) == ("contradicts", "direct", "gov")
    assert official.rationale == CONTRADICTS.rationale
    assert (lead.relation, lead.status, lead.source_kind) == ("context", "retrieval_failed", "search")
    assert (lead.excerpt, lead.rationale) == ("", None)
    assert all("3 所學校" not in record.excerpt for record in result.evidence)
    # The assessor saw the located claim text and was never shown the unreadable lead.
    assert assessor.calls == [("市府表示補助 1,000 萬元", OFFICIAL, "factual_contradiction")]
    assert fetcher.calls == [OFFICIAL, LEAD]


def test_analyze_revision_without_an_assessor_shows_nothing():
    from gaohe.analysis import analyze_revision

    item = e2e_revision()

    result = analyze_revision(item, (), e2e_analysis(item), e2e_search(OFFICIAL), LookupFetcher(e2e_pages()))

    assert [finding.visible for finding in result.findings] == [False, False]
    assert result.evidence[0].status == "retrieved"
    assert (result.evidence[0].relation, result.evidence[0].rationale) == ("context", None)


def test_analyze_revision_keeps_conflicting_sources_pending_for_human_reading():
    from gaohe.analysis import analyze_revision

    item = e2e_revision()
    supports = EvidenceAssessment("supports", "文件記載補助 1,000 萬元。", "補助 1,000 萬元")
    assessor = ScriptedAssessor({OFFICIAL: CONTRADICTS, SUPPORT: supports})

    result = analyze_revision(item, (), e2e_analysis(item), e2e_search(OFFICIAL, SUPPORT), LookupFetcher(e2e_pages()), assessor)

    first = result.findings[0]
    assert (first.visible, first.status, first.evidence_status) == (False, "pending", "retrieved")
    assert [(record.relation, record.source_kind) for record in result.evidence[:2]] == [
        ("contradicts", "direct"), ("supports", "firecrawl"),
    ]


def test_analyze_revision_keeps_an_ungrounded_contradiction_unassessed():
    from gaohe.analysis import analyze_revision

    item = e2e_revision()
    fabricated = EvidenceAssessment("contradicts", "頁面說補助 5 萬元。", "本案補助 5 萬元")

    assessor = ScriptedAssessor({OFFICIAL: fabricated})

    result = analyze_revision(item, (), e2e_analysis(item), e2e_search(OFFICIAL), LookupFetcher(e2e_pages()), assessor)

    assert result.findings[0].visible is False
    assert (result.evidence[0].relation, result.evidence[0].rationale) == ("context", None)


def test_analyze_revision_survives_an_assessor_that_always_fails():
    from gaohe.analysis import analyze_revision

    class Broken:
        def assess(self, *_args):
            raise ValueError("Gemini assessment request failed (HTTP 503)")

    item = e2e_revision()

    result = analyze_revision(item, (), e2e_analysis(item), e2e_search(OFFICIAL), LookupFetcher(e2e_pages()), Broken())

    assert [finding.visible for finding in result.findings] == [False, False]
    assert len(result.findings) == 2
