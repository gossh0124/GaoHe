from dataclasses import dataclass
from hashlib import sha256
import unicodedata


CLAIM_KINDS = frozenset({"checkable", "descriptive", "attributed_statement", "inference", "opinion"})
CLAIM_MATERIALITIES = frozenset({"ordinary", "material"})
CLAIM_EXTRACTION_STATUSES = frozenset({"extracted", "rejected"})
MAX_QUERY_CHARS = 500
MAX_SEARCH_LIMIT = 10
# Machine-side lifecycle of one revision's analysis job.
ANALYSIS_STATUSES = frozenset({"pending", "running", "completed", "failed", "skipped"})
# Human review of a finding; independent from the machine-side finding status.
REVIEW_STATUSES = frozenset({"unreviewed", "confirmed", "dismissed"})
# What an evidence assessor may conclude about one retrieved page.
ASSESSMENT_RELATIONS = frozenset({"supports", "contradicts", "context", "irrelevant"})


@dataclass(frozen=True)
class Source:
    id: int | None
    name: str
    feed_url: str
    article_url: str | None = None
    enabled: bool = True
    kind: str = "feed"


@dataclass(frozen=True)
class ArticleCandidate:
    source_id: int
    url: str
    title: str
    published_at: str | None
    discovered_at: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class FetchedArticle:
    candidate: ArticleCandidate
    text: str
    fetched_at: str
    content_hash: str
    fetch_status: str = "ok"


@dataclass(frozen=True)
class ArticleRevision:
    id: int
    article_id: int
    url: str
    title: str
    text: str
    content_hash: str
    fetched_at: str


@dataclass(frozen=True)
class Claim:
    id: int | None
    revision_id: int
    text: str
    start: int
    end: int
    kind: str
    materiality: str
    extraction_status: str


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    source: str
    published_at: str | None


@dataclass(frozen=True)
class RetrievedPage:
    url: str
    title: str
    text: str
    retrieved_at: str
    status: str
    content_hash: str | None
    fetched_via: str = "direct"


@dataclass(frozen=True)
class Evidence:
    id: int | None
    finding_id: int | None
    url: str
    title: str
    excerpt: str
    relation: str
    status: str
    source_kind: str
    retrieved_at: str | None
    provider: str | None = None
    published_at: str | None = None
    content_hash: str | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class EvidenceAssessment:
    """An assessor's reading of one retrieved page against one article claim."""

    relation: str
    rationale: str
    evidence_quote: str


@dataclass(frozen=True)
class Finding:
    id: int | None
    revision_id: int
    claim_id: int | None
    finding_type: str
    summary: str
    start: int
    end: int
    status: str
    evidence_status: str
    visible: bool
    review_status: str = "unreviewed"


@dataclass(frozen=True)
class FindingCandidate:
    claim_id: int | None
    finding_type: str
    summary: str
    start: int
    end: int
    materiality: str
    query: str | None
    revision_id: int | None = None


@dataclass(frozen=True)
class AnalysisResult:
    revision_id: int
    claims: tuple[Claim, ...]
    candidates: tuple[FindingCandidate, ...]
    evidence: tuple[Evidence, ...] = ()
    findings: tuple[Finding, ...] = ()
    rejected_claims: int = 0


@dataclass(frozen=True)
class TopicGroup:
    id: int | None
    label: str
    confidence: str
    status: str


@dataclass(frozen=True)
class CheckOutcome:
    """Result of checking one user-supplied article URL on demand (never an article verdict)."""

    status: str  # completed | failed | skipped | invalid_url | fetch_failed
    message: str  # plain zh-TW explanation of what happened and what to do next
    revision_id: int | None = None
    article_id: int | None = None


@dataclass(frozen=True)
class RunSummary:
    started_at: str
    finished_at: str | None
    sources_checked: int
    candidates_seen: int
    revisions_created: int
    failures: int


def normalize_article_content(title: str, text: str) -> tuple[str, str]:
    """Return the NFC, LF-normalized article title and text."""
    return (
        unicodedata.normalize("NFC", title.replace("\r\n", "\n").replace("\r", "\n")),
        unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")),
    )


def article_content_hash(title: str, text: str) -> str:
    """Return the SHA-256 for NFC, LF-normalized title and article text."""
    normalized_title, normalized_text = normalize_article_content(title, text)
    normalized = f"{normalized_title}\n{normalized_text}"
    return sha256(normalized.encode("utf-8")).hexdigest()
