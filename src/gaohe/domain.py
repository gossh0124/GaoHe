from dataclasses import dataclass
from hashlib import sha256
import unicodedata


@dataclass(frozen=True)
class Source:
    id: int | None
    name: str
    feed_url: str
    article_url: str | None = None
    enabled: bool = True


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
class RunSummary:
    started_at: str
    finished_at: str | None
    sources_checked: int
    candidates_seen: int
    revisions_created: int
    failures: int


def article_content_hash(title: str, text: str) -> str:
    """Return the SHA-256 for NFC, LF-normalized title and article text."""
    normalized = unicodedata.normalize("NFC", f"{title}\n{text}".replace("\r\n", "\n").replace("\r", "\n"))
    return sha256(normalized.encode("utf-8")).hexdigest()
