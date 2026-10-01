from dataclasses import dataclass
from hashlib import sha256
import unicodedata


CLAIM_KINDS = frozenset({"checkable", "descriptive", "attributed_statement", "inference", "opinion"})
CLAIM_MATERIALITIES = frozenset({"ordinary", "material"})
# The only annotation types that can ever become visible (spec 7.3, without the cross-media type).
FINDING_TYPES = frozenset({"factual_contradiction", "unsupported_inference"})
# What an evidence assessor may conclude about one retrieved page.
ASSESSMENT_RELATIONS = frozenset({"supports", "contradicts", "context", "irrelevant"})
MAX_QUERY_CHARS = 500


@dataclass(frozen=True)
class Source:
    id: int | None
    name: str
    feed_url: str
    article_url: str | None = None
    enabled: bool = True
    kind: str = "feed"  # "feed" is polled; "manual" owns single articles checked on demand


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


@dataclass(frozen=True)
class FindingCandidate:
    finding_type: str
    summary: str
    start: int
    end: int
    query: str | None
    revision_id: int


@dataclass(frozen=True)
class AnalysisResult:
    revision_id: int
    claims: tuple[Claim, ...]
    candidates: tuple[FindingCandidate, ...]
    rejected_claims: int = 0


@dataclass(frozen=True)
class SearchHit:
    """A search lead: where to look, never evidence by itself."""

    url: str
    title: str
    source: str


@dataclass(frozen=True)
class RetrievedPage:
    url: str
    title: str
    text: str
    retrieved_at: str
    status: str  # "retrieved" or a failure reason
    content_hash: str | None


@dataclass(frozen=True)
class Evidence:
    url: str
    title: str
    excerpt: str
    relation: str  # supports | contradicts | context
    status: str  # retrieved | retrieval_failed | insufficient_scope
    retrieved_at: str | None
    provider: str | None = None
    rationale: str | None = None  # set only by an assessor


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
    finding_type: str
    summary: str
    start: int
    end: int
    evidence_status: str
    visible: bool


@dataclass(frozen=True)
class CheckOutcome:
    """Result of checking one user-supplied article URL on demand (never an article verdict)."""

    status: str  # completed | failed | invalid_url | fetch_failed
    message: str  # plain zh-TW explanation of what happened and what to do next
    revision_id: int | None = None


@dataclass(frozen=True)
class RunSummary:
    started_at: str
    finished_at: str
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
    return sha256(f"{normalized_title}\n{normalized_text}".encode("utf-8")).hexdigest()
