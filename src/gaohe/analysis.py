from .domain import ArticleRevision, Claim, normalize_article_content
from .providers import AnalysisProvider, AnalysisResult, FindingCandidate


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
        and all(isinstance(value, str) and 0 < len(value) <= _MAX_FIELD_CHARS for value in (claim.kind, claim.materiality, claim.extraction_status))
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


def extract_claims(revision: ArticleRevision, provider: AnalysisProvider) -> AnalysisResult:
    """Validate provider output and retain only material candidate proposals.

    This deliberately creates neither evidence nor visible findings; those are
    resolved by the later evidence stage.
    """
    _, normalized_text = normalize_article_content(revision.title, revision.text)
    if revision.text != normalized_text:
        raise ValueError("revision text must be normalized")

    result = provider.analyze(revision, ())
    if result.revision_id != revision.id:
        raise ValueError("provider result revision_id does not match revision")
    if not all(_valid_claim(claim, revision) for claim in result.claims):
        raise ValueError("provider claim text or span is invalid")

    candidates = tuple(
        candidate
        for candidate in result.candidates
        if _valid_candidate_shape(candidate, revision)
        and _has_claim_association(candidate, result.claims, revision)
    )
    return AnalysisResult(revision.id, result.claims, candidates)
