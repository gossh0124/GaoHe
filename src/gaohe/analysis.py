from collections.abc import Sequence
import re
from urllib.parse import urlsplit, urlunsplit

from .domain import CLAIM_EXTRACTION_STATUSES, CLAIM_KINDS, CLAIM_MATERIALITIES, ArticleRevision, Claim, Evidence, Finding, RetrievedPage, SearchHit, normalize_article_content
from .providers import AnalysisProvider, AnalysisResult, EvidenceSearchProvider, FindingCandidate, MAX_QUERY_CHARS, MAX_SEARCH_LIMIT, PageFetcher
from .storage import MAX_EVIDENCE_EXCERPT_CHARS, redact_text, redact_url


_ALLOWED_FINDING_TYPES = frozenset({
    "factual_contradiction",
    "material_cross_media_difference",
    "unsupported_inference",
})
_MAX_CLAIM_CHARS = 2_000
_MAX_FIELD_CHARS = 100
_MAX_SUMMARY_CHARS = 2_000


def allowed_finding_type(value: str) -> bool:
    return isinstance(value, str) and value in _ALLOWED_FINDING_TYPES


def _valid_span(text: str, start: object, end: object) -> bool:
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(text)
    )


def _valid_claim(claim: Claim, revision: ArticleRevision) -> bool:
    text = revision.text
    return (
        claim.revision_id == revision.id
        and _valid_span(text, claim.start, claim.end)
        and isinstance(claim.text, str)
        and 0 < len(claim.text) <= _MAX_CLAIM_CHARS
        and text[claim.start:claim.end] == claim.text
        and claim.kind in CLAIM_KINDS
        and claim.materiality in CLAIM_MATERIALITIES
        and claim.extraction_status in CLAIM_EXTRACTION_STATUSES
    )


def _valid_candidate_shape(candidate: FindingCandidate, revision: ArticleRevision) -> bool:
    return (
        _valid_span(revision.text, candidate.start, candidate.end)
        and allowed_finding_type(candidate.finding_type)
        and isinstance(candidate.summary, str)
        and bool(candidate.summary.strip())
        and len(candidate.summary) <= _MAX_SUMMARY_CHARS
        and candidate.materiality == "material"
    )


def is_material_candidate(claim: Claim, candidate: FindingCandidate, revision: ArticleRevision) -> bool:
    """Return whether a provider proposal clears the pre-evidence materiality gate."""
    if not _valid_claim(claim, revision) or not _valid_candidate_shape(candidate, revision):
        return False
    if claim.materiality != "material" or claim.kind in {"opinion", "descriptive"}:
        return False
    if (candidate.start, candidate.end) != (claim.start, claim.end):
        return False
    return candidate.claim_id is None or candidate.claim_id == claim.id


def _has_claim_association(candidate: FindingCandidate, claims: tuple[Claim, ...], revision: ArticleRevision) -> bool:
    matches = tuple(claim for claim in claims if is_material_candidate(claim, candidate, revision))
    return len(matches) == 1


def extract_claims(revision: ArticleRevision, provider: AnalysisProvider, related: Sequence[ArticleRevision] = ()) -> AnalysisResult:
    """Validate provider output and retain only material candidate proposals.

    This deliberately creates neither evidence nor visible findings; those are
    resolved by the later evidence stage.
    """
    _, normalized_text = normalize_article_content(revision.title, revision.text)
    if revision.text != normalized_text:
        raise ValueError("revision text must be normalized")

    result = provider.analyze(revision, related)
    if result.revision_id != revision.id:
        raise ValueError("provider result revision_id does not match revision")
    if not all(_valid_claim(claim, revision) for claim in result.claims):
        raise ValueError("provider claim text or span is invalid")
    spans = [(claim.start, claim.end) for claim in result.claims]
    if len(set(spans)) != len(spans):
        raise ValueError("duplicate claim span in provider result")

    candidates = tuple(
        candidate
        for candidate in result.candidates
        if _valid_candidate_shape(candidate, revision)
        and _has_claim_association(candidate, result.claims, revision)
    )
    return AnalysisResult(revision.id, result.claims, candidates)


def _canonical_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _query(candidate: FindingCandidate, revision: ArticleRevision | None = None) -> str:
    query = (candidate.query or candidate.summary).strip()
    if re.search(r"(?i)(?:https?://[^\s/@]+:[^\s/@]+@|\b(?:api[-_]?key|(?:access|refresh|client)[-_]?(?:token|secret)|token|secret|password|passwd|pwd|cookie|authorization)\s*(?:=|:))", query):
        return ""
    query = " ".join(query.split())
    if revision is not None and query and query in " ".join(revision.text.split()):
        return ""
    return query[:MAX_QUERY_CHARS]


def _evidence_status(page: RetrievedPage) -> str:
    if page.status == "retrieved" and page.url and page.text.strip():
        return "retrieved"
    if page.status in {"parse_error", "oversized", "retrieved"}:
        return "insufficient_scope"
    return "retrieval_failed"


def retrieve_evidence(candidate: FindingCandidate, search: EvidenceSearchProvider, fetcher: PageFetcher, limit: int = 5, revision: ArticleRevision | None = None) -> list[Evidence]:
    """Fetch bounded, deduplicated full-text evidence; search snippets never become evidence."""
    query = _query(candidate, revision)
    if not query:
        return []
    bounded_limit = min(max(1, limit), MAX_SEARCH_LIMIT)
    hits: list[SearchHit] = []
    seen: set[str] = set()
    try:
        search_hits = search.search(query, bounded_limit)
    except Exception:
        return []
    for hit in search_hits:
        canonical = _canonical_url(hit.url)
        if canonical is not None and canonical not in seen:
            seen.add(canonical)
            hits.append(hit)
            if len(hits) == bounded_limit:
                break
    result: list[Evidence] = []
    for hit in hits:
        try:
            page = fetcher.fetch(hit.url)
        except Exception:
            result.append(Evidence(None, None, redact_url(canonical), hit.title, "", "context", "retrieval_failed", "search", None, hit.source, hit.published_at, None))
            continue
        status = _evidence_status(page)
        page_url = _canonical_url(page.url)
        if status == "retrieved" and page_url is not None:
            result.append(Evidence(None, None, redact_url(page_url), page.title, redact_text(page.text, MAX_EVIDENCE_EXCERPT_CHARS), "context", "retrieved", "search", page.retrieved_at, hit.source, hit.published_at, page.content_hash))
        else:
            result.append(Evidence(None, None, redact_url(canonical), hit.title, "", "context", status, "search", page.retrieved_at, hit.source, hit.published_at, None))
    return result


def _usable(evidence: Evidence, relations: set[str], source_kind: str | None = None) -> bool:
    return evidence.status == "retrieved" and evidence.relation in relations and _canonical_url(evidence.url) is not None and bool(evidence.excerpt.strip()) and (source_kind is None or evidence.source_kind == source_kind)


def resolve_finding(candidate: FindingCandidate, evidence: Sequence[Evidence], related: Sequence[ArticleRevision], revision: ArticleRevision | None = None) -> Finding:
    """Resolve only explicit, retrievable full-text evidence; otherwise remain pending."""
    if candidate.revision_id is None or candidate.revision_id < 1:
        raise ValueError("candidate revision_id is required")
    if revision is not None and candidate.revision_id != revision.id:
        raise ValueError("candidate revision_id does not match revision")
    if candidate.finding_type == "factual_contradiction":
        visible = any(_usable(item, {"contradicts"}) for item in evidence)
    elif candidate.finding_type == "material_cross_media_difference":
        related_urls = {_canonical_url(item.url) for item in related}
        related_urls.discard(None)
        visible = any(_usable(item, {"contradicts"}, "related_article") and _canonical_url(item.url) in related_urls for item in evidence)
    elif candidate.finding_type == "unsupported_inference":
        visible = any(_usable(item, {"context", "contradicts"}) and item.source_kind != "search" for item in evidence)
    else:
        visible = False
    statuses = {item.status for item in evidence}
    evidence_status = "retrieved" if any(item.status == "retrieved" for item in evidence) else ("retrieval_failed" if "retrieval_failed" in statuses else ("insufficient_scope" if "insufficient_scope" in statuses else "pending"))
    return Finding(None, candidate.revision_id, candidate.claim_id, candidate.finding_type, candidate.summary, candidate.start, candidate.end, "resolved" if visible else "pending", evidence_status, visible)


def analyze_revision(revision: ArticleRevision, related: Sequence[ArticleRevision], analysis: AnalysisProvider, search: EvidenceSearchProvider, fetcher: PageFetcher) -> AnalysisResult:
    extracted = extract_claims(revision, analysis, related)
    if any(candidate.revision_id not in {None, revision.id} for candidate in extracted.candidates):
        raise ValueError("candidate revision_id does not match revision")
    candidates = tuple(candidate if candidate.revision_id == revision.id else FindingCandidate(candidate.claim_id, candidate.finding_type, candidate.summary, candidate.start, candidate.end, candidate.materiality, candidate.query, revision.id) for candidate in extracted.candidates)
    evidence_batches = tuple(retrieve_evidence(candidate, search, fetcher, revision=revision) for candidate in candidates)
    evidence = tuple(item for batch in evidence_batches for item in batch)
    findings = tuple(resolve_finding(candidate, batch, related, revision) for candidate, batch in zip(candidates, evidence_batches))
    return AnalysisResult(revision.id, extracted.claims, candidates, evidence, findings)
