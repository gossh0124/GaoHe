from collections.abc import Sequence
from dataclasses import replace
import re

from .domain import (
    ASSESSMENT_RELATIONS,
    CLAIM_EXTRACTION_STATUSES,
    CLAIM_KINDS,
    CLAIM_MATERIALITIES,
    MAX_QUERY_CHARS,
    MAX_SEARCH_LIMIT,
    AnalysisResult,
    ArticleRevision,
    Claim,
    Evidence,
    EvidenceAssessment,
    Finding,
    FindingCandidate,
    RetrievedPage,
    SearchHit,
    normalize_article_content,
)
from .policy import FINDING_TYPES, FULL_TEXT_SOURCE_KINDS, is_visible
from .providers import (
    AnalysisProvider,
    EvidenceAssessor,
    EvidenceSearchProvider,
    PageFetcher,
    contains_quote,
    locate_quote as locate_quote,  # re-exported: quote anchoring is part of this module's API
)
from .safety import MAX_EVIDENCE_EXCERPT_CHARS, canonical_url as _canonical_url, redact_text, redact_url


_MAX_CLAIM_CHARS = 2_000
_MAX_SUMMARY_CHARS = 2_000
MAX_RATIONALE_CHARS = 1_000
_SECRET_QUERY = re.compile(
    r"(?i)(?:https?://[^\s/@]+:[^\s/@]+@"
    r"|\b(?:api[-_]?key|(?:access|refresh|client)[-_]?(?:token|secret)|token|secret|password|passwd|pwd|cookie|authorization)\s*(?:=|:))"
)


def allowed_finding_type(value: str) -> bool:
    return isinstance(value, str) and value in FINDING_TYPES


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
        isinstance(claim, Claim)
        and claim.revision_id == revision.id
        and _valid_span(text, claim.start, claim.end)
        and isinstance(claim.text, str)
        and 0 < len(claim.text) <= _MAX_CLAIM_CHARS
        and bool(claim.text.strip())
        and text[claim.start:claim.end] == claim.text
        and claim.kind in CLAIM_KINDS
        and claim.materiality in CLAIM_MATERIALITIES
        and claim.extraction_status in CLAIM_EXTRACTION_STATUSES
    )


def _valid_candidate_shape(candidate: FindingCandidate, revision: ArticleRevision) -> bool:
    return (
        isinstance(candidate, FindingCandidate)
        and _valid_span(revision.text, candidate.start, candidate.end)
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


def _associated_claims(candidate: FindingCandidate, claims: Sequence[Claim], revision: ArticleRevision) -> tuple[Claim, ...]:
    return tuple(claim for claim in claims if is_material_candidate(claim, candidate, revision))


def _provider_rejections(result: AnalysisResult) -> int:
    value = result.rejected_claims
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def extract_claims(revision: ArticleRevision, provider: AnalysisProvider, related: Sequence[ArticleRevision] = ()) -> AnalysisResult:
    """Validate provider output and retain only material candidate proposals.

    Invalid, unlocatable, and duplicate-span claims are dropped (never raised) and
    counted in rejected_claims, together with the claims the provider itself could
    not anchor; candidates tied to a dropped claim are dropped with it. This
    deliberately creates neither evidence nor visible findings; those are resolved
    by the later evidence stage.
    """
    _, normalized_text = normalize_article_content(revision.title, revision.text)
    if revision.text != normalized_text:
        raise ValueError("revision text must be normalized")

    result = provider.analyze(revision, related)
    if result.revision_id != revision.id:
        raise ValueError("provider result revision_id does not match revision")

    kept: list[Claim] = []
    spans: set[tuple[int, int]] = set()
    rejected = _provider_rejections(result)
    for claim in result.claims:
        if not _valid_claim(claim, revision) or (claim.start, claim.end) in spans:
            rejected += 1
            continue
        spans.add((claim.start, claim.end))
        kept.append(claim)
    claims = tuple(kept)

    candidates = tuple(
        candidate
        for candidate in result.candidates
        if _valid_candidate_shape(candidate, revision)
        and len(_associated_claims(candidate, claims, revision)) == 1
    )
    return AnalysisResult(revision.id, claims, candidates, rejected_claims=rejected)


def _query(candidate: FindingCandidate, revision: ArticleRevision | None = None) -> str:
    query = (candidate.query or candidate.summary).strip()
    if _SECRET_QUERY.search(query):
        return ""
    query = " ".join(query.split())
    if revision is not None and query:
        article_text = " ".join(revision.text.split())
        if query in article_text or article_text in query:
            return ""
    return query[:MAX_QUERY_CHARS]


def _evidence_status(page: RetrievedPage) -> str:
    if page.status == "retrieved" and page.url and page.text.strip():
        return "retrieved"
    if page.status in {"parse_error", "oversized", "retrieved"}:
        return "insufficient_scope"
    return "retrieval_failed"


def _unfetched(hit: SearchHit, canonical: str, status: str, retrieved_at: str | None) -> Evidence:
    """A search hit whose page could not be read: a lead only, so no excerpt and source_kind "search"."""
    return Evidence(
        None, None, redact_url(canonical), hit.title, "", "context", status, "search",
        retrieved_at, hit.source, hit.published_at, None,
    )


def retrieve_evidence(
    candidate: FindingCandidate,
    search: EvidenceSearchProvider,
    fetcher: PageFetcher,
    limit: int = 5,
    revision: ArticleRevision | None = None,
) -> list[Evidence]:
    """Fetch bounded, deduplicated full-text evidence; search snippets never become evidence.

    A fetched page is recorded with source_kind page.fetched_via ("direct" or
    "firecrawl"); every record starts unassessed (relation "context", no rationale).
    """
    query = _query(candidate, revision)
    if not query:
        return []
    bounded_limit = min(max(1, limit), MAX_SEARCH_LIMIT)
    hits: list[tuple[SearchHit, str]] = []
    seen: set[str] = set()
    try:
        search_hits = search.search(query, bounded_limit)
    except Exception:
        return []
    for hit in search_hits:
        canonical = _canonical_url(hit.url)
        if canonical is not None and canonical not in seen:
            seen.add(canonical)
            hits.append((hit, canonical))
            if len(hits) == bounded_limit:
                break
    result: list[Evidence] = []
    for hit, canonical in hits:
        try:
            page = fetcher.fetch(hit.url)
        except Exception:
            result.append(_unfetched(hit, canonical, "retrieval_failed", None))
            continue
        status = _evidence_status(page)
        page_url = _canonical_url(page.url)
        if status != "retrieved" or page_url is None:
            result.append(_unfetched(hit, canonical, status, page.retrieved_at))
            continue
        # An unknown fetch path is not trusted as full-text provenance.
        source_kind = page.fetched_via if page.fetched_via in FULL_TEXT_SOURCE_KINDS else "search"
        result.append(Evidence(
            None, None, redact_url(page_url), page.title,
            redact_text(page.text, MAX_EVIDENCE_EXCERPT_CHARS), "context", "retrieved", source_kind,
            page.retrieved_at, hit.source, hit.published_at, page.content_hash,
        ))
    return result


def _unassessed(evidence: Evidence) -> Evidence:
    return replace(evidence, relation="context", rationale=None)


def _assessed(evidence: Evidence, assessment: object) -> Evidence:
    """Apply one assessor answer, or leave the evidence unassessed when it does not hold up."""
    if not isinstance(assessment, EvidenceAssessment):
        return _unassessed(evidence)
    relation, quote = assessment.relation, assessment.evidence_quote
    if not isinstance(relation, str) or relation not in ASSESSMENT_RELATIONS or not isinstance(quote, str):
        return _unassessed(evidence)
    rationale = redact_text(assessment.rationale, MAX_RATIONALE_CHARS).strip()
    if not rationale:
        return _unassessed(evidence)
    # The quote must be traceable to the excerpt; only "irrelevant" may omit it.
    if (quote.strip() or relation != "irrelevant") and not contains_quote(evidence.excerpt, quote):
        return _unassessed(evidence)
    if relation == "irrelevant":
        return replace(evidence, relation="context", status="insufficient_scope", rationale=rationale)
    return replace(evidence, relation=relation, rationale=rationale)


def assess_evidence(
    candidate: FindingCandidate,
    claim_text: str,
    revision: ArticleRevision,
    evidence_batch: Sequence[Evidence],
    assessor: EvidenceAssessor | None,
) -> list[Evidence]:
    """Ask the assessor how each retrieved full-text page relates to the claim.

    Only "retrieved" evidence with a non-empty excerpt is assessed; everything else
    is returned unchanged. A missing assessor, an assessor error, an unknown
    relation, an empty rationale, or a quote not found in the excerpt all leave the
    page unassessed (relation "context", rationale None), which is never visible.
    """
    result: list[Evidence] = []
    for evidence in evidence_batch:
        if evidence.status != "retrieved" or not isinstance(evidence.excerpt, str) or not evidence.excerpt.strip():
            result.append(evidence)
            continue
        if assessor is None:
            result.append(_unassessed(evidence))
            continue
        try:
            assessment = assessor.assess(claim_text, revision, evidence, candidate.finding_type)
        except Exception:
            assessment = None
        result.append(_assessed(evidence, assessment))
    return result


def _pending_evidence_status(evidence: Sequence[Evidence]) -> str:
    statuses = {item.status for item in evidence}
    for status in ("retrieved", "retrieval_failed", "insufficient_scope"):
        if status in statuses:
            return status
    return "pending"


def resolve_finding(
    candidate: FindingCandidate,
    evidence: Sequence[Evidence],
    related: Sequence[ArticleRevision],
    revision: ArticleRevision | None = None,
) -> Finding:
    """Resolve only explicit, assessed full-text evidence (see gaohe.policy); otherwise remain pending."""
    if candidate.revision_id is None or candidate.revision_id < 1:
        raise ValueError("candidate revision_id is required")
    if revision is not None and candidate.revision_id != revision.id:
        raise ValueError("candidate revision_id does not match revision")
    visible = is_visible(candidate.finding_type, evidence, [item.url for item in related])
    return Finding(
        None, candidate.revision_id, candidate.claim_id, candidate.finding_type, candidate.summary,
        candidate.start, candidate.end, "resolved" if visible else "pending",
        _pending_evidence_status(evidence), visible,
    )


def _claim_text(candidate: FindingCandidate, claims: Sequence[Claim], revision: ArticleRevision) -> str:
    matches = _associated_claims(candidate, claims, revision)
    return matches[0].text if matches else revision.text[candidate.start:candidate.end]


def analyze_revision(
    revision: ArticleRevision,
    related: Sequence[ArticleRevision],
    analysis: AnalysisProvider,
    search: EvidenceSearchProvider,
    fetcher: PageFetcher,
    assessor: EvidenceAssessor | None = None,
) -> AnalysisResult:
    """Extract -> retrieve -> assess -> resolve, one candidate at a time.

    Without an assessor every page stays unassessed, so nothing becomes visible.
    """
    extracted = extract_claims(revision, analysis, related)
    if any(candidate.revision_id not in {None, revision.id} for candidate in extracted.candidates):
        raise ValueError("candidate revision_id does not match revision")
    candidates = tuple(replace(candidate, revision_id=revision.id) for candidate in extracted.candidates)
    evidence_batches = tuple(
        tuple(assess_evidence(
            candidate,
            _claim_text(candidate, extracted.claims, revision),
            revision,
            retrieve_evidence(candidate, search, fetcher, revision=revision),
            assessor,
        ))
        for candidate in candidates
    )
    evidence = tuple(item for batch in evidence_batches for item in batch)
    findings = tuple(
        resolve_finding(candidate, batch, related, revision)
        for candidate, batch in zip(candidates, evidence_batches)
    )
    return AnalysisResult(
        revision.id, extracted.claims, candidates, evidence, findings, rejected_claims=extracted.rejected_claims,
    )
