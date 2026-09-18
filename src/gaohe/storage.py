import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from .domain import FetchedArticle, RunSummary, Source, article_content_hash


_SENSITIVE_HEADER = re.compile(r"(?im)^[^\r\n:]*?(?:authorization|cookie|token|secret|password|session|api[-_]key)[^\r\n:]*:\s*[^\r\n]*")
_SENSITIVE_QUERY = re.compile(r"(?i)([?&][^=&#\s]*(?:authorization|cookie|token|secret|password|session|api[-_]key)[^=&#\s]*=)[^&#\s]*")
_SENSITIVE_VALUE = re.compile(r"(?i)\b(?:api[_-]?key|(?:access|refresh|client)[_-]?(?:token|secret)|token|secret|password|passwd|pwd|session(?:[_-]?id)?)\s*=\s*[^\s,;&]+")
_BEARER_TOKEN = re.compile(r"(?i)bearer\s+[^\s,;]+")


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
                    metadata_json TEXT NOT NULL,
                    current_revision_id INTEGER REFERENCES article_revisions(id)
                );
                CREATE TABLE IF NOT EXISTS article_revisions (
                    id INTEGER PRIMARY KEY,
                    article_id INTEGER NOT NULL REFERENCES articles(id),
                    text TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    fetch_status TEXT NOT NULL
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
            columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
            if "current_revision_id" not in columns:
                self._migrate_revisions(connection)
                connection.execute("ALTER TABLE articles ADD COLUMN current_revision_id INTEGER REFERENCES article_revisions(id)")
                connection.execute(
                    """UPDATE articles SET current_revision_id = (
                       SELECT id FROM article_revisions
                       WHERE article_id = articles.id ORDER BY id DESC LIMIT 1)"""
                )

    @staticmethod
    def _migrate_revisions(connection: sqlite3.Connection) -> None:
        indexes = list(connection.execute("PRAGMA index_list(article_revisions)"))
        if not any(index[2] for index in indexes):
            return
        connection.executescript(
            """
            ALTER TABLE article_revisions RENAME TO article_revisions_legacy;
            CREATE TABLE article_revisions (
                id INTEGER PRIMARY KEY,
                article_id INTEGER NOT NULL REFERENCES articles(id),
                text TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                fetch_status TEXT NOT NULL
            );
            INSERT INTO article_revisions (id, article_id, text, fetched_at, content_hash, fetch_status)
            SELECT id, article_id, text, fetched_at, content_hash, fetch_status FROM article_revisions_legacy;
            DROP TABLE article_revisions_legacy;
            """
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

    def save_fetched_article(self, article: FetchedArticle) -> tuple[int, bool]:
        expected_hash = article_content_hash(article.candidate.title, article.text)
        if article.content_hash != expected_hash:
            raise ValueError("content_hash must match normalized title and text")
        candidate = article.candidate
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO articles (source_id, url, title, published_at, discovered_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET source_id = excluded.source_id, title = excluded.title,
                     published_at = excluded.published_at, discovered_at = excluded.discovered_at,
                     metadata_json = excluded.metadata_json""",
                (candidate.source_id, candidate.url, candidate.title, _utc_iso(candidate.published_at) if candidate.published_at else None,
                 _utc_iso(candidate.discovered_at), json.dumps(candidate.metadata, sort_keys=True, ensure_ascii=False)),
            )
            article_id, current_revision_id = connection.execute(
                "SELECT id, current_revision_id FROM articles WHERE url = ?", (candidate.url,)
            ).fetchone()
            if current_revision_id is not None:
                current_hash = connection.execute(
                    "SELECT content_hash FROM article_revisions WHERE id = ?", (current_revision_id,)
                ).fetchone()[0]
                if current_hash == article.content_hash:
                    return current_revision_id, False
            cursor = connection.execute(
                """INSERT INTO article_revisions (article_id, text, fetched_at, content_hash, fetch_status)
                   VALUES (?, ?, ?, ?, ?)""",
                (article_id, article.text, _utc_iso(article.fetched_at), article.content_hash, article.fetch_status),
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
