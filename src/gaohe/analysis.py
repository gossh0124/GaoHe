"""One revision: extract claims -> search leads -> fetch full text -> assess -> decide visibility.

Visibility is decided only in ``is_visible``. Every rule is conservative: a finding stays
pending unless assessed, retrievable full-text evidence carries it. Missing, failed,
snippet-only or unassessed evidence never makes a finding visible, and a visible finding is
never a verdict on the article or the outlet.
"""

from collections.abc import Iterable, Sequence
from dataclasses import replace
import re
from urllib.parse import urlsplit

from .domain import (
    AnalysisResult,
    ArticleRevision,
    Claim,
    Evidence,
    EvidenceAssessment,
    Finding,
    FindingCandidate,
    normalize_article_content,
)
from .providers import AnalysisProvider, EvidenceAssessor, PageFetcher, SearchProvider, contains_quote
from .safety import MAX_EVIDENCE_EXCERPT_CHARS, canonical_url, redact_text, redact_url


MAX_EVIDENCE_PAGES = 5
MAX_RATIONALE_CHARS = 1_000
# An evidence quote shorter than this cannot ground anything (a single 的 or 是 matches every page).
MIN_EVIDENCE_QUOTE_CHARS = 6
_SECRET_QUERY = re.compile(
    r"(?i)(?:https?://[^\s/@]+:[^\s/@]+@|\b(?:api[-_]?key|token|secret|password|cookie|authorization)\s*[=:])"
)


def extract_claims(revision: ArticleRevision, provider: AnalysisProvider) -> AnalysisResult:
    """Provider claims anchored by quote; unusable ones are already dropped and counted by the provider."""
    if revision.text != normalize_article_content(revision.title, revision.text)[1]:
        raise ValueError("revision text must be normalized")
    result = provider.analyze(revision)
    if result.revision_id != revision.id:
        raise ValueError("provider result revision_id does not match revision")
    return result


def _query(candidate: FindingCandidate, revision: ArticleRevision) -> str:
    query = " ".join((candidate.query or candidate.summary).split())
    if not query or _SECRET_QUERY.search(query) or query in " ".join(revision.text.split()):
        return ""  # never send credentials or the article itself to a search engine
    return query


def retrieve_evidence(
    candidate: FindingCandidate, revision: ArticleRevision, search: SearchProvider, fetcher: PageFetcher
) -> list[Evidence]:
    """Fetch the full text behind each search lead; a lead whose page cannot be read is never evidence."""
    query = _query(candidate, revision)
    if not query:
        return []
    try:
        hits = search.search(query, MAX_EVIDENCE_PAGES)
    except Exception:
        return []
    evidence: list[Evidence] = []
    seen: set[str] = set()
    for hit in hits[:MAX_EVIDENCE_PAGES]:
        try:
            page = fetcher.fetch(hit.url)
        except Exception:
            page = None
        url = canonical_url(page.url) if page is not None else None
        if page is None or page.status != "retrieved" or url is None or not page.text.strip():
            evidence.append(Evidence(redact_url(hit.url), hit.title, "", "context", "retrieval_failed", None, hit.source))
            continue
        if url in seen:
            continue
        seen.add(url)
        evidence.append(Evidence(
            redact_url(url), redact_text(page.title, 500), redact_text(page.text, MAX_EVIDENCE_EXCERPT_CHARS),
            "context", "retrieved", page.retrieved_at, hit.source,
        ))
    return evidence


def _assessed(evidence: Evidence, assessment: object) -> Evidence:
    """Apply one assessor answer, or leave the evidence unassessed when it does not hold up."""
    if not isinstance(assessment, EvidenceAssessment):
        return evidence
    rationale = redact_text(assessment.rationale, MAX_RATIONALE_CHARS).strip()
    quote = assessment.evidence_quote.strip() if isinstance(assessment.evidence_quote, str) else ""
    if not rationale:
        return evidence
    if assessment.relation == "irrelevant":
        return replace(evidence, status="insufficient_scope", rationale=rationale)
    # The quote must be a real, traceable passage of the excerpt.
    if len(quote) < MIN_EVIDENCE_QUOTE_CHARS or not contains_quote(evidence.excerpt, quote):
        return evidence
    return replace(evidence, relation=assessment.relation, rationale=rationale)


def assess_evidence(
    candidate: FindingCandidate,
    claim_text: str,
    revision: ArticleRevision,
    evidence: Sequence[Evidence],
    assessor: EvidenceAssessor | None,
) -> list[Evidence]:
    """Ask the assessor about each retrieved page; any failure leaves that page unassessed."""
    result: list[Evidence] = []
    for item in evidence:
        if assessor is None or item.status != "retrieved" or not item.excerpt.strip():
            result.append(item)
            continue
        try:
            assessment = assessor.assess(candidate, claim_text, revision, item)
        except Exception:
            assessment = None
        result.append(_assessed(item, assessment))
    return result


def _usable(evidence: Evidence) -> bool:
    return (
        evidence.status == "retrieved"  # failed or blocked retrieval is never evidence (spec 2.4)
        and canonical_url(evidence.url) is not None  # the source must be traceable (spec 6.3)
        and bool(evidence.excerpt.strip())  # a citable page text, never a bare snippet (spec 2.3)
        and bool(evidence.rationale)  # only assessed evidence has a relation (spec 7.1)
    )


def is_visible(finding_type: str, evidence: Iterable[Evidence]) -> bool:
    """The one place that decides whether a finding may become a visible annotation."""
    items = [item for item in evidence if _usable(item)]
    if any(item.relation == "supports" for item in items):
        return False  # sources disagree: stays pending for a human reader (spec 6.4)
    if finding_type == "factual_contradiction":
        return any(item.relation == "contradicts" for item in items)
    if finding_type == "unsupported_inference":
        # One page that merely lacks the conclusion proves little; require two independent sites.
        hosts = {urlsplit(item.url).hostname for item in items if item.relation in ("context", "contradicts")}
        return len(hosts) >= 2
    return False


def _evidence_status(evidence: Sequence[Evidence]) -> str:
    statuses = {item.status for item in evidence}
    for status in ("retrieved", "retrieval_failed", "insufficient_scope"):
        if status in statuses:
            return status
    return "pending"


def _claim_text(candidate: FindingCandidate, claims: Sequence[Claim], revision: ArticleRevision) -> str:
    for claim in claims:
        if (claim.start, claim.end) == (candidate.start, candidate.end):
            return claim.text
    return revision.text[candidate.start:candidate.end]


def analyze_revision(
    revision: ArticleRevision,
    analysis: AnalysisProvider,
    search: SearchProvider,
    fetcher: PageFetcher,
    assessor: EvidenceAssessor | None,
) -> tuple[AnalysisResult, list[Finding], list[list[Evidence]]]:
    """Extract -> retrieve -> assess -> resolve, one candidate at a time."""
    extracted = extract_claims(revision, analysis)
    findings: list[Finding] = []
    batches: list[list[Evidence]] = []
    for candidate in extracted.candidates:
        claim_text = _claim_text(candidate, extracted.claims, revision)
        evidence = assess_evidence(
            candidate, claim_text, revision, retrieve_evidence(candidate, revision, search, fetcher), assessor
        )
        findings.append(Finding(
            None, revision.id, candidate.finding_type, candidate.summary, candidate.start, candidate.end,
            _evidence_status(evidence), is_visible(candidate.finding_type, evidence),
        ))
        batches.append(evidence)
    return extracted, findings, batches
