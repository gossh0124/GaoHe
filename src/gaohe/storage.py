import json
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .domain import ArticleRevision, Claim, Evidence, FetchedArticle, Finding, RunSummary, Source, TopicGroup, article_content_hash, normalize_article_content


_SENSITIVE_NAME = r"(?:authorization|cookie|token|secret|password|session|api[-_]key)"
_SENSITIVE_HEADER = re.compile(rf"(?im)^[^\r\n:]*?{_SENSITIVE_NAME}[^\r\n:]*:\s*[^\r\n]*")
_SENSITIVE_QUERY = re.compile(rf"(?i)([?&][^=&#\s]*{_SENSITIVE_NAME}[^=&#\s]*=)[^&#\s]*")
_SENSITIVE_FRAGMENT = re.compile(rf"(?i)(^|[?&])([^=&#\s]*{_SENSITIVE_NAME}[^=&#\s]*=)[^&#\s]*")
_SENSITIVE_VALUE = re.compile(r"(?i)\b(?:api[_-]?key|(?:access|refresh|client)[_-]?(?:token|secret)|token|secret|password|passwd|pwd|session(?:[_-]?id)?)\s*=\s*[^\s,;&]+")
_BEARER_TOKEN = re.compile(r"(?i)bearer\s+[^\s,;]+")
_PROVIDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CLAIM_KINDS = {"checkable", "descriptive"}
_MATERIALITIES = {"ordinary", "material"}
_EXTRACTION_STATUSES = {"extracted", "rejected"}
_EVIDENCE_RELATIONS = {"supports", "contradicts", "context"}
_EVIDENCE_STATUSES = {"pending", "retrieved", "retrieval_failed", "insufficient_scope"}
_EVIDENCE_SOURCE_KINDS = {"direct", "search", "firecrawl", "related_article"}
_FINDING_TYPES = {"factual_contradiction", "material_cross_media_difference", "unsupported_inference"}
_FINDING_STATUSES = {"pending", "resolved", "dismissed"}
_TOPIC_CONFIDENCES = {"high", "possible", "low"}
_TOPIC_STATUSES = {"active", "possible", "dismissed"}


def _require_allowed(name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise ValueError(f"unsupported {name}: {value}")


def _utc_iso(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamps must be UTC ISO-8601 strings")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_error(error: str | None) -> str | None:
    if error is None:
        return None
    redacted = _SENSITIVE_HEADER.sub("[redacted]", error)
    redacted = _SENSITIVE_QUERY.sub(r"\1[redacted]", redacted)
    redacted = _SENSITIVE_VALUE.sub(lambda match: match.group(0).split("=", 1)[0] + "=[redacted]", redacted)
    return _BEARER_TOKEN.sub("Bearer [redacted]", redacted)[:500]


def redact_url(value: str) -> str:
    parsed = urlsplit(value)
    query = urlencode([
        (key, "***" if re.search(_SENSITIVE_NAME, key, re.IGNORECASE) else query_value)
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
    ])
    fragment = _SENSITIVE_FRAGMENT.sub(r"\1\2***", parsed.fragment)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, query, fragment))


def _safe_provider_name(value: str | None) -> str | None:
    return value if value and _PROVIDER_NAME.fullmatch(value) else None


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    feed_url TEXT NOT NULL UNIQUE,
                    article_url TEXT,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS source_checks (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES sources(id),
                    checked_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    candidates_seen INTEGER NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES sources(id),
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    published_at TEXT,
                    discovered_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    sources_checked INTEGER NOT NULL,
                    candidates_seen INTEGER NOT NULL,
                    revisions_created INTEGER NOT NULL,
                    failures INTEGER NOT NULL
                );
                """
            )
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'article_revisions'").fetchone():
                self._migrate_revisions(connection)
            else:
                connection.execute(
                    """CREATE TABLE article_revisions (
                        id INTEGER PRIMARY KEY,
                        article_id INTEGER NOT NULL REFERENCES articles(id),
                        title TEXT NOT NULL,
                        text TEXT NOT NULL,
                        fetched_at TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        fetch_status TEXT NOT NULL
                    )"""
                )
            revision_columns = {row[1] for row in connection.execute("PRAGMA table_info(article_revisions)")}
            if "title" not in revision_columns:
                connection.execute("ALTER TABLE article_revisions ADD COLUMN title TEXT")
                connection.execute("UPDATE article_revisions SET title = (SELECT title FROM articles WHERE articles.id = article_revisions.article_id)")
            self._normalize_revisions(connection)
            article_columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
            if "current_revision_id" not in article_columns:
                connection.execute("ALTER TABLE articles ADD COLUMN current_revision_id INTEGER REFERENCES article_revisions(id)")
                connection.execute("""UPDATE articles SET current_revision_id = (
                    SELECT id FROM article_revisions WHERE article_id = articles.id ORDER BY id DESC LIMIT 1)""")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY,
                    revision_id INTEGER NOT NULL REFERENCES article_revisions(id),
                    text TEXT NOT NULL,
                    start INTEGER NOT NULL,
                    end INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    materiality TEXT NOT NULL,
                    extraction_status TEXT NOT NULL,
                    UNIQUE (revision_id, start, end)
                );
                CREATE TABLE IF NOT EXISTS findings (
                    id INTEGER PRIMARY KEY,
                    revision_id INTEGER NOT NULL REFERENCES article_revisions(id),
                    claim_id INTEGER REFERENCES claims(id),
                    finding_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    start INTEGER NOT NULL,
                    end INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    evidence_status TEXT NOT NULL,
                    visible INTEGER NOT NULL CHECK (visible IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY,
                    finding_id INTEGER REFERENCES findings(id),
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    retrieved_at TEXT,
                    provider TEXT,
                    published_at TEXT,
                    content_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS topics (
                    id INTEGER PRIMARY KEY,
                    label TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topic_articles (
                    topic_id INTEGER NOT NULL REFERENCES topics(id),
                    revision_id INTEGER NOT NULL REFERENCES article_revisions(id),
                    PRIMARY KEY (topic_id, revision_id)
                );
                CREATE TABLE IF NOT EXISTS revision_analysis (
                    revision_id INTEGER PRIMARY KEY REFERENCES article_revisions(id),
                    status TEXT NOT NULL CHECK (status = 'completed')
                );
                """
            )
            evidence_columns = {row[1] for row in connection.execute("PRAGMA table_info(evidence)")}
            for column in ("provider", "published_at", "content_hash"):
                if column not in evidence_columns:
                    connection.execute(f"ALTER TABLE evidence ADD COLUMN {column} TEXT")

    @staticmethod
    def _migrate_revisions(connection: sqlite3.Connection) -> None:
        indexes = list(connection.execute("PRAGMA index_list(article_revisions)"))
        if not any(index[2] for index in indexes):
            return
        connection.execute("ALTER TABLE article_revisions RENAME TO article_revisions_legacy")
        connection.execute(
            """CREATE TABLE article_revisions (
                id INTEGER PRIMARY KEY,
                article_id INTEGER NOT NULL REFERENCES articles(id),
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                fetch_status TEXT NOT NULL
            )"""
        )
        revisions = connection.execute(
            """SELECT revisions.id, revisions.article_id, articles.title, revisions.text,
                      revisions.fetched_at, revisions.fetch_status
               FROM article_revisions_legacy AS revisions JOIN articles ON articles.id = revisions.article_id"""
        )
        connection.executemany(
            """INSERT INTO article_revisions (id, article_id, title, text, fetched_at, content_hash, fetch_status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (revision_id, article_id, *normalize_article_content(title, text), fetched_at,
                 article_content_hash(title, text), fetch_status)
                for revision_id, article_id, title, text, fetched_at, fetch_status in revisions
            ],
        )
        connection.execute("DROP TABLE article_revisions_legacy")

    @staticmethod
    def _normalize_revisions(connection: sqlite3.Connection) -> None:
        for revision_id, title, text, content_hash in connection.execute(
            "SELECT id, title, text, content_hash FROM article_revisions"
        ):
            normalized_title, normalized_text = normalize_article_content(title, text)
            normalized_hash = article_content_hash(normalized_title, normalized_text)
            if (normalized_title, normalized_text, normalized_hash) != (title, text, content_hash):
                connection.execute(
                    "UPDATE article_revisions SET title = ?, text = ?, content_hash = ? WHERE id = ?",
                    (normalized_title, normalized_text, normalized_hash, revision_id),
                )

    def add_source(self, source: Source) -> int:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO sources (name, feed_url, article_url, enabled) VALUES (?, ?, ?, ?)
                   ON CONFLICT(feed_url) DO UPDATE SET
                     name = excluded.name, article_url = excluded.article_url, enabled = excluded.enabled""",
                (source.name, source.feed_url, source.article_url, int(source.enabled)),
            )
            return connection.execute("SELECT id FROM sources WHERE feed_url = ?", (source.feed_url,)).fetchone()[0]

    def list_sources(self, enabled_only: bool = False) -> list[Source]:
        with self._connection() as connection:
            query = "SELECT id, name, feed_url, article_url, enabled FROM sources"
            if enabled_only:
                query += " WHERE enabled = 1"
            return [Source(row[0], row[1], row[2], row[3], bool(row[4])) for row in connection.execute(query)]

    def set_source_enabled(self, source_id: int, enabled: bool) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), source_id))
            return cursor.rowcount == 1

    @staticmethod
    def _save_candidate(connection: sqlite3.Connection, candidate) -> tuple[int, int | None]:
        connection.execute(
            """INSERT INTO articles (source_id, url, title, published_at, discovered_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET source_id = excluded.source_id, title = excluded.title,
                 published_at = excluded.published_at, discovered_at = excluded.discovered_at,
                 metadata_json = excluded.metadata_json""",
            (candidate.source_id, candidate.url, candidate.title, _utc_iso(candidate.published_at) if candidate.published_at else None,
             _utc_iso(candidate.discovered_at), json.dumps(candidate.metadata, sort_keys=True, ensure_ascii=False)),
        )
        return connection.execute(
            "SELECT id, current_revision_id FROM articles WHERE url = ?", (candidate.url,)
        ).fetchone()

    def save_candidate(self, candidate) -> None:
        with self._connection() as connection:
            self._save_candidate(connection, candidate)

    def save_fetched_article(self, article: FetchedArticle) -> tuple[int, bool]:
        expected_hash = article_content_hash(article.candidate.title, article.text)
        if article.content_hash != expected_hash:
            raise ValueError("content_hash must match normalized title and text")
        title, text = normalize_article_content(article.candidate.title, article.text)
        candidate = replace(article.candidate, title=title)
        with self._connection() as connection:
            article_id, current_revision_id = self._save_candidate(connection, candidate)
            if current_revision_id is not None:
                current_hash = connection.execute(
                    "SELECT content_hash FROM article_revisions WHERE id = ?", (current_revision_id,)
                ).fetchone()[0]
                if current_hash == article.content_hash:
                    return current_revision_id, False
            cursor = connection.execute(
                """INSERT INTO article_revisions (article_id, title, text, fetched_at, content_hash, fetch_status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (article_id, title, text, _utc_iso(article.fetched_at), article.content_hash, article.fetch_status),
            )
            connection.execute("UPDATE articles SET current_revision_id = ? WHERE id = ?", (cursor.lastrowid, article_id))
            return cursor.lastrowid, True

    def record_source_check(self, source_id: int, checked_at: str, status: str, candidates_seen: int, error: str | None) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO source_checks (source_id, checked_at, status, candidates_seen, error) VALUES (?, ?, ?, ?, ?)",
                (source_id, _utc_iso(checked_at), status, candidates_seen, _safe_error(error)),
            )

    def record_run(self, summary: RunSummary) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO runs (started_at, finished_at, sources_checked, candidates_seen, revisions_created, failures)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_utc_iso(summary.started_at), _utc_iso(summary.finished_at) if summary.finished_at else None,
                 summary.sources_checked, summary.candidates_seen, summary.revisions_created, summary.failures),
            )
            return cursor.lastrowid

    def latest_content_hash(self, url: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT revisions.content_hash FROM articles
                   JOIN article_revisions AS revisions ON revisions.id = articles.current_revision_id
                   WHERE articles.url = ?""",
                (url,),
            ).fetchone()
            return row[0] if row else None

    def latest_article_metadata(self, url: str) -> dict[str, str] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT metadata_json FROM articles WHERE url = ?", (url,)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _revision(connection: sqlite3.Connection, revision_id: int) -> ArticleRevision | None:
        row = connection.execute(
            """SELECT revisions.id, revisions.article_id, articles.url, revisions.title,
                      revisions.text, revisions.content_hash, revisions.fetched_at
               FROM article_revisions AS revisions
               JOIN articles ON articles.id = revisions.article_id
               WHERE revisions.id = ?""",
            (revision_id,),
        ).fetchone()
        return ArticleRevision(*row) if row else None

    def save_claims(self, revision_id: int, claims: Sequence[Claim]) -> list[int]:
        with self._connection() as connection:
            revision = self._revision(connection, revision_id)
            if revision is None:
                raise ValueError("unknown revision")
            claim_ids = []
            for claim in claims:
                if claim.revision_id != revision_id:
                    raise ValueError("claim revision_id must match revision_id")
                if not 0 <= claim.start <= claim.end <= len(revision.text):
                    raise ValueError("claim span is outside the normalized article text")
                _require_allowed("claim kind", claim.kind, _CLAIM_KINDS)
                _require_allowed("claim materiality", claim.materiality, _MATERIALITIES)
                _require_allowed("claim extraction_status", claim.extraction_status, _EXTRACTION_STATUSES)
                try:
                    cursor = connection.execute(
                        """INSERT INTO claims (revision_id, text, start, end, kind, materiality, extraction_status)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (revision_id, claim.text, claim.start, claim.end, claim.kind, claim.materiality, claim.extraction_status),
                    )
                except sqlite3.IntegrityError as error:
                    if "claims.revision_id, claims.start, claims.end" in str(error):
                        raise ValueError("duplicate claim span") from error
                    raise
                claim_ids.append(cursor.lastrowid)
            connection.execute(
                "INSERT INTO revision_analysis (revision_id, status) VALUES (?, 'completed') ON CONFLICT(revision_id) DO UPDATE SET status = excluded.status",
                (revision_id,),
            )
            return claim_ids

    def save_evidence(self, evidence: Evidence) -> int:
        _require_allowed("evidence relation", evidence.relation, _EVIDENCE_RELATIONS)
        _require_allowed("evidence status", evidence.status, _EVIDENCE_STATUSES)
        _require_allowed("evidence source_kind", evidence.source_kind, _EVIDENCE_SOURCE_KINDS)
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO evidence (finding_id, url, title, excerpt, relation, status, source_kind, retrieved_at, provider, published_at, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (evidence.finding_id, redact_url(evidence.url),
                 evidence.title if evidence.status == "retrieved" else f"Evidence {evidence.status.replace('_', ' ')}",
                 evidence.excerpt if evidence.status == "retrieved" else "", evidence.relation, evidence.status,
                 evidence.source_kind, _utc_iso(evidence.retrieved_at) if evidence.retrieved_at else None, _safe_provider_name(evidence.provider),
                 _utc_iso(evidence.published_at) if evidence.published_at else None, evidence.content_hash),
            )
            return cursor.lastrowid

    def save_finding(self, finding: Finding) -> int:
        _require_allowed("finding_type", finding.finding_type, _FINDING_TYPES)
        _require_allowed("finding status", finding.status, _FINDING_STATUSES)
        _require_allowed("finding evidence_status", finding.evidence_status, _EVIDENCE_STATUSES)
        with self._connection() as connection:
            revision = self._revision(connection, finding.revision_id)
            if revision is None:
                raise ValueError("unknown revision")
            if not 0 <= finding.start <= finding.end <= len(revision.text):
                raise ValueError("finding span is outside the normalized article text")
            if finding.claim_id is not None:
                row = connection.execute("SELECT revision_id FROM claims WHERE id = ?", (finding.claim_id,)).fetchone()
                if row is None or row[0] != finding.revision_id:
                    raise ValueError("finding claim must belong to its revision")
            cursor = connection.execute(
                """INSERT INTO findings (revision_id, claim_id, finding_type, summary, start, end, status, evidence_status, visible)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (finding.revision_id, finding.claim_id, finding.finding_type, finding.summary, finding.start, finding.end,
                 finding.status, finding.evidence_status, int(finding.visible)),
            )
            return cursor.lastrowid

    def link_revision_to_topic(self, revision_id: int, topic_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO topic_articles (topic_id, revision_id) VALUES (?, ?)",
                (topic_id, revision_id),
            )

    def save_topic(self, topic: TopicGroup) -> int:
        _require_allowed("topic confidence", topic.confidence, _TOPIC_CONFIDENCES)
        _require_allowed("topic status", topic.status, _TOPIC_STATUSES)
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO topics (label, confidence, status) VALUES (?, ?, ?)",
                (topic.label, topic.confidence, topic.status),
            )
            return cursor.lastrowid

    def list_pending_revisions(self, limit: int = 20) -> list[ArticleRevision]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connection() as connection:
            return [ArticleRevision(*row) for row in connection.execute(
                """SELECT revisions.id, revisions.article_id, articles.url, revisions.title,
                          revisions.text, revisions.content_hash, revisions.fetched_at
                   FROM article_revisions AS revisions
                   JOIN articles ON articles.id = revisions.article_id
                   WHERE NOT EXISTS (SELECT 1 FROM revision_analysis WHERE revision_analysis.revision_id = revisions.id AND revision_analysis.status = 'completed')
                   ORDER BY revisions.id LIMIT ?""",
                (limit,),
            )]

    def list_topic_revisions(self, topic_id: int) -> list[ArticleRevision]:
        with self._connection() as connection:
            return [ArticleRevision(*row) for row in connection.execute(
                """SELECT revisions.id, revisions.article_id, articles.url, revisions.title,
                          revisions.text, revisions.content_hash, revisions.fetched_at
                   FROM topic_articles
                   JOIN article_revisions AS revisions ON revisions.id = topic_articles.revision_id
                   JOIN articles ON articles.id = revisions.article_id
                   WHERE topic_articles.topic_id = ? ORDER BY revisions.id""",
                (topic_id,),
            )]
