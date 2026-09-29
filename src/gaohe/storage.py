"""Local SQLite store for monitoring, analysis jobs, human review and read models.

The schema is versioned with ``PRAGMA user_version`` and upgraded by ordered,
numbered migrations, each applied in its own transaction. Every URL or
free-form text that may carry credentials goes through ``gaohe.safety`` before
it is written or returned, so nothing here can leak an API key into SQLite,
logs or HTML.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from .safety import MAX_EVIDENCE_EXCERPT_CHARS, redact_text, redact_url, safe_error as _safe_error  # noqa: F401 - re-exported for callers
from .domain import (
    ANALYSIS_STATUSES,
    CLAIM_EXTRACTION_STATUSES,
    CLAIM_KINDS,
    CLAIM_MATERIALITIES,
    REVIEW_STATUSES,
    ArticleRevision,
    Claim,
    Evidence,
    FetchedArticle,
    Finding,
    RunSummary,
    Source,
    TopicGroup,
    article_content_hash,
    normalize_article_content,
)


BUSY_TIMEOUT_MS = 5_000
MAX_RATIONALE_CHARS = 1_000
MAX_REVIEW_NOTE_CHARS = 500
MAX_HTTP_VALIDATOR_CHARS = 200
MAX_EVIDENCE_TITLE_CHARS = 500
MAX_TOPIC_LABEL_CHARS = 500

_PROVIDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_EVIDENCE_RELATIONS = {"supports", "contradicts", "context"}
_EVIDENCE_STATUSES = {"pending", "retrieved", "retrieval_failed", "insufficient_scope"}
_EVIDENCE_SOURCE_KINDS = {"direct", "search", "firecrawl", "related_article"}
_FINDING_TYPES = {"factual_contradiction", "material_cross_media_difference", "unsupported_inference"}
_FINDING_STATUSES = {"pending", "resolved", "dismissed"}
_TOPIC_CONFIDENCES = {"high", "possible", "low"}
_TOPIC_STATUSES = {"active", "possible", "dismissed"}
_SQL_CHUNK = 500
_REVISION_COLUMNS = """revisions.id, revisions.article_id, articles.url, revisions.title,
                       revisions.text, revisions.content_hash, revisions.fetched_at"""
_ANALYSIS_STATUS_KEYS = ("status", "attempts", "last_error", "updated_at", "provider", "model", "prompt_version")
_ARTICLE_FETCH_KEYS = (
    "etag", "last_modified", "last_fetched_at", "fetch_count", "unchanged_count",
    "published_at", "discovered_at", "content_hash",
)
_RUN_KEYS = ("started_at", "finished_at", "sources_checked", "candidates_seen", "revisions_created", "failures")
_EVIDENCE_KEYS = ("url", "title", "provider", "retrieved_at", "relation", "status", "source_kind", "rationale")


class SchemaVersionError(sqlite3.DatabaseError):
    """The database was written by a newer GaoHe schema than this code understands."""


def _require_allowed(name: str, value: str, allowed: set[str] | frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"unsupported {name}: {value}")


def _utc_iso(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamps must be UTC ISO-8601 strings")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_utc_iso(value: str | None) -> str | None:
    return _utc_iso(value) if value else None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _moment(value: str | datetime | None) -> datetime:
    """Return an aware UTC datetime; None means the real clock (tests inject a value)."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamps must be UTC ISO-8601 strings")
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(_utc_iso(value).replace("Z", "+00:00"))


def _sortable(value: str) -> str:
    """Fixed-width, lexically ordered form of a UTC timestamp, to microsecond precision."""
    return _moment(value).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _sortable_sql(column: str) -> str:
    # _utc_iso() writes "YYYY-MM-DDTHH:MM:SSZ" or "YYYY-MM-DDTHH:MM:SS.ffffffZ"; julianday() would round to ms.
    return f"(substr({column}, 1, 19) || CASE WHEN length({column}) = 27 THEN substr({column}, 20, 7) ELSE '.000000' END)"


def _safe_provider_name(value: str | None) -> str | None:
    return value if isinstance(value, str) and _PROVIDER_NAME.fullmatch(value) else None


def _required_label(name: str, value: str) -> str:
    if not isinstance(value, str) or not _PROVIDER_NAME.fullmatch(value):
        raise ValueError(f"unsupported {name}")
    return value


def _bounded_text(value: object, limit: int) -> str | None:
    """Redact and bound optional free text; blank text is stored as NULL."""
    text = redact_text(value, limit)
    return text if text.strip() else None


def _error_text(error: object) -> str | None:
    if error is None:
        return None
    return _safe_error(error if isinstance(error, str) else str(error))


def _http_validator(value: object) -> str | None:
    """Keep an ETag or Last-Modified value only when it is short, printable and credential-free."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_HTTP_VALIDATOR_CHARS:
        return None
    if any(unicodedata.category(character)[0] in {"C", "Z"} and character != " " for character in value):
        return None
    return value if redact_text(value, MAX_HTTP_VALIDATOR_CHARS) == value else None


def _display_url(value: object) -> str:
    """Return a URL safe to hand to the web layer; unparseable values become empty."""
    if not isinstance(value, str):
        return ""
    try:
        return redact_url(value)
    except ValueError:
        return ""


def _placeholders(values: Sequence[object]) -> str:
    return ", ".join("?" for _ in values)


def _chunks(values: Sequence[int]) -> Iterator[Sequence[int]]:
    for offset in range(0, len(values), _SQL_CHUNK):
        yield values[offset:offset + _SQL_CHUNK]


# --- schema migrations -------------------------------------------------------

_BASE_TABLES = (
    """CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        feed_url TEXT NOT NULL UNIQUE,
        article_url TEXT,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
    )""",
    """CREATE TABLE IF NOT EXISTS source_checks (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        checked_at TEXT NOT NULL,
        status TEXT NOT NULL,
        candidates_seen INTEGER NOT NULL,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        url TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        published_at TEXT,
        discovered_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        sources_checked INTEGER NOT NULL,
        candidates_seen INTEGER NOT NULL,
        revisions_created INTEGER NOT NULL,
        failures INTEGER NOT NULL
    )""",
)

_REVISIONS_TABLE = """CREATE TABLE article_revisions (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fetch_status TEXT NOT NULL
)"""

# The analysis tables exactly as the pre-versioning code created them; later
# migrations change them so that fresh and upgraded databases share one path.
_BASELINE_ANALYSIS_TABLES = (
    """CREATE TABLE IF NOT EXISTS claims (
        id INTEGER PRIMARY KEY,
        revision_id INTEGER NOT NULL REFERENCES article_revisions(id),
        text TEXT NOT NULL,
        start INTEGER NOT NULL,
        end INTEGER NOT NULL,
        kind TEXT NOT NULL,
        materiality TEXT NOT NULL,
        extraction_status TEXT NOT NULL,
        UNIQUE (revision_id, start, end)
    )""",
    """CREATE TABLE IF NOT EXISTS findings (
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
    )""",
    """CREATE TABLE IF NOT EXISTS evidence (
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
    )""",
    """CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY,
        label TEXT NOT NULL,
        confidence TEXT NOT NULL,
        status TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS topic_articles (
        topic_id INTEGER NOT NULL REFERENCES topics(id),
        revision_id INTEGER NOT NULL REFERENCES article_revisions(id),
        PRIMARY KEY (topic_id, revision_id)
    )""",
    """CREATE TABLE IF NOT EXISTS revision_analysis (
        revision_id INTEGER PRIMARY KEY REFERENCES article_revisions(id),
        status TEXT NOT NULL CHECK (status = 'completed')
    )""",
)


def _execute_all(connection: sqlite3.Connection, statements: Sequence[str]) -> None:
    # executescript() would COMMIT the migration transaction early, so run one statement at a time.
    for statement in statements:
        connection.execute(statement)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _user_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def _migrate_legacy_revisions(connection: sqlite3.Connection) -> None:
    """Drop the legacy UNIQUE(article_id, content_hash) index by rebuilding article_revisions."""
    indexes = list(connection.execute("PRAGMA index_list(article_revisions)"))
    if not any(index[2] for index in indexes):
        return
    connection.execute("ALTER TABLE article_revisions RENAME TO article_revisions_legacy")
    connection.execute(_REVISIONS_TABLE)
    revisions = connection.execute(
        """SELECT revisions.id, revisions.article_id, articles.title, revisions.text,
                  revisions.fetched_at, revisions.fetch_status
           FROM article_revisions_legacy AS revisions JOIN articles ON articles.id = revisions.article_id"""
    ).fetchall()
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


def _normalize_revisions(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT id, title, text, content_hash FROM article_revisions").fetchall()
    for revision_id, title, text, content_hash in rows:
        normalized_title, normalized_text = normalize_article_content(title, text)
        normalized_hash = article_content_hash(normalized_title, normalized_text)
        if (normalized_title, normalized_text, normalized_hash) != (title, text, content_hash):
            connection.execute(
                "UPDATE article_revisions SET title = ?, text = ?, content_hash = ? WHERE id = ?",
                (normalized_title, normalized_text, normalized_hash, revision_id),
            )


def _migration_1_baseline(connection: sqlite3.Connection) -> None:
    """Create the v0 schema, or upgrade any database written before schema versioning to it."""
    _execute_all(connection, _BASE_TABLES)
    if _table_exists(connection, "article_revisions"):
        # Must run before any table referencing article_revisions exists: RENAME rewrites child FKs.
        _migrate_legacy_revisions(connection)
    else:
        connection.execute(_REVISIONS_TABLE)
    if "title" not in _columns(connection, "article_revisions"):
        connection.execute("ALTER TABLE article_revisions ADD COLUMN title TEXT")
        connection.execute(
            """UPDATE article_revisions
               SET title = (SELECT title FROM articles WHERE articles.id = article_revisions.article_id)"""
        )
    _normalize_revisions(connection)
    if "current_revision_id" not in _columns(connection, "articles"):
        connection.execute("ALTER TABLE articles ADD COLUMN current_revision_id INTEGER REFERENCES article_revisions(id)")
        connection.execute(
            """UPDATE articles SET current_revision_id = (
                   SELECT id FROM article_revisions WHERE article_id = articles.id ORDER BY id DESC LIMIT 1)"""
        )
    _execute_all(connection, _BASELINE_ANALYSIS_TABLES)
    for column in ("provider", "published_at", "content_hash"):
        _add_column(connection, "evidence", column, "TEXT")


def _migration_2_analysis_jobs(connection: sqlite3.Connection) -> None:
    """Rebuild revision_analysis as a job table; SQLite cannot drop the old completed-only CHECK."""
    if "attempts" in _columns(connection, "revision_analysis"):
        return
    connection.execute(
        """CREATE TABLE revision_analysis_new (
            revision_id INTEGER PRIMARY KEY REFERENCES article_revisions(id),
            status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT,
            provider TEXT,
            model TEXT,
            prompt_version TEXT
        )"""
    )
    connection.execute(
        """INSERT INTO revision_analysis_new (revision_id, status)
           SELECT revision_id, 'completed' FROM revision_analysis
           WHERE revision_id IN (SELECT id FROM article_revisions)"""
    )
    connection.execute("DROP TABLE revision_analysis")
    connection.execute("ALTER TABLE revision_analysis_new RENAME TO revision_analysis")


def _migration_3_review_fetch_state_and_ledger(connection: sqlite3.Connection) -> None:
    _add_column(connection, "findings", "review_status", "TEXT NOT NULL DEFAULT 'unreviewed'")
    _add_column(connection, "findings", "reviewed_at", "TEXT")
    _add_column(connection, "findings", "review_note", "TEXT")
    _add_column(connection, "evidence", "rationale", "TEXT")
    for column in ("etag", "last_modified", "last_fetched_at"):
        _add_column(connection, "articles", column, "TEXT")
    _add_column(connection, "articles", "fetch_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, "articles", "unchanged_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, "sources", "etag", "TEXT")
    _add_column(connection, "sources", "last_modified", "TEXT")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY,
            called_at TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            purpose TEXT NOT NULL,
            revision_id INTEGER REFERENCES article_revisions(id),
            status TEXT NOT NULL,
            input_chars INTEGER NOT NULL DEFAULT 0,
            output_chars INTEGER NOT NULL DEFAULT 0
        )"""
    )


def _migration_4_indexes(connection: sqlite3.Connection) -> None:
    _execute_all(connection, (
        "CREATE INDEX IF NOT EXISTS idx_article_revisions_article ON article_revisions(article_id)",
        "CREATE INDEX IF NOT EXISTS idx_findings_revision ON findings(revision_id)",
        "CREATE INDEX IF NOT EXISTS idx_evidence_finding ON evidence(finding_id)",
        "CREATE INDEX IF NOT EXISTS idx_source_checks_source_checked ON source_checks(source_id, checked_at)",
        "CREATE INDEX IF NOT EXISTS idx_llm_calls_called ON llm_calls(called_at)",
        "CREATE INDEX IF NOT EXISTS idx_topic_articles_revision ON topic_articles(revision_id)",
    ))


# Append-only: never edit or reorder a released step; add a new one instead.
_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migration_1_baseline,
    _migration_2_analysis_jobs,
    _migration_3_review_fetch_state_and_ledger,
    _migration_4_indexes,
)
SCHEMA_VERSION = len(_MIGRATIONS)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self, *, autocommit: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None if autocommit else "",
        )
        # foreign_keys is a no-op inside a transaction, so set both pragmas before BEGIN.
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return connection

    @contextmanager
    def _connection(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """One transaction per call: IMMEDIATE for writes (no lock-upgrade races), deferred snapshot for reads."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create or upgrade the database; calling it again on a current database changes nothing."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect(autocommit=True)
        try:
            self._enable_wal(connection)
            self._migrate(connection)
        finally:
            connection.close()

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        # The scheduled watch and the local web page may use the database at the same time.
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            return
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # Another process holds the file; busy_timeout still serializes access and a later run retries.
            return

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        version = _user_version(connection)
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(f"database schema version {version} is newer than supported version {SCHEMA_VERSION}")
        for number, migration in enumerate(_MIGRATIONS, start=1):
            if number <= version:
                continue
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Re-read under the write lock: a concurrent initialize() may have applied this step.
                current = _user_version(connection)
                if current > SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"database schema version {current} is newer than supported version {SCHEMA_VERSION}"
                    )
                if current < number:
                    migration(connection)
                    connection.execute(f"PRAGMA user_version = {number}")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    # --- sources and articles ------------------------------------------------

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
        with self._connection(write=False) as connection:
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
        # First-seen time and the discovering source are facts; a later listing only refreshes metadata.
        connection.execute(
            """INSERT INTO articles (source_id, url, title, published_at, discovered_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                 title = excluded.title,
                 published_at = COALESCE(excluded.published_at, articles.published_at),
                 metadata_json = excluded.metadata_json""",
            (
                candidate.source_id,
                candidate.url,
                candidate.title,
                _optional_utc_iso(candidate.published_at),
                _utc_iso(candidate.discovered_at),
                json.dumps(candidate.metadata, sort_keys=True, ensure_ascii=False),
            ),
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
                (_utc_iso(summary.started_at), _optional_utc_iso(summary.finished_at),
                 summary.sources_checked, summary.candidates_seen, summary.revisions_created, summary.failures),
            )
            return cursor.lastrowid

    def latest_content_hash(self, url: str) -> str | None:
        with self._connection(write=False) as connection:
            row = connection.execute(
                """SELECT revisions.content_hash FROM articles
                   JOIN article_revisions AS revisions ON revisions.id = articles.current_revision_id
                   WHERE articles.url = ?""",
                (url,),
            ).fetchone()
            return row[0] if row else None

    def latest_article_metadata(self, url: str) -> dict[str, str] | None:
        with self._connection(write=False) as connection:
            row = connection.execute("SELECT metadata_json FROM articles WHERE url = ?", (url,)).fetchone()
            return json.loads(row[0]) if row else None

    # --- conditional-fetch state for the monitor -----------------------------

    def article_fetch_state(self, url: str) -> dict[str, object] | None:
        with self._connection(write=False) as connection:
            row = connection.execute(
                """SELECT articles.etag, articles.last_modified, articles.last_fetched_at, articles.fetch_count,
                          articles.unchanged_count, articles.published_at, articles.discovered_at, revisions.content_hash
                   FROM articles
                   LEFT JOIN article_revisions AS revisions ON revisions.id = articles.current_revision_id
                   WHERE articles.url = ?""",
                (url,),
            ).fetchone()
        return dict(zip(_ARTICLE_FETCH_KEYS, row)) if row else None

    def record_article_fetch(
        self,
        url: str,
        fetched_at: str,
        *,
        changed: bool,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> bool:
        """Count one fetch of a known article; validators are replaced only by a new, valid value."""
        fetched = _utc_iso(fetched_at)
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE articles SET
                     last_fetched_at = ?,
                     fetch_count = fetch_count + 1,
                     unchanged_count = CASE WHEN ? THEN 0 ELSE unchanged_count + 1 END,
                     etag = COALESCE(?, etag),
                     last_modified = COALESCE(?, last_modified)
                   WHERE url = ?""",
                (fetched, int(bool(changed)), _http_validator(etag), _http_validator(last_modified), url),
            )
            return cursor.rowcount == 1

    def source_fetch_state(self, source_id: int) -> dict[str, str | None] | None:
        with self._connection(write=False) as connection:
            row = connection.execute("SELECT etag, last_modified FROM sources WHERE id = ?", (source_id,)).fetchone()
        return {"etag": row[0], "last_modified": row[1]} if row else None

    def record_source_fetch_state(self, source_id: int, etag: str | None, last_modified: str | None) -> bool:
        """Replace a source's HTTP validators; invalid values are dropped rather than stored."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE sources SET etag = ?, last_modified = ? WHERE id = ?",
                (_http_validator(etag), _http_validator(last_modified), source_id),
            )
            return cursor.rowcount == 1

    # --- claims, findings and evidence ---------------------------------------

    @staticmethod
    def _revision(connection: sqlite3.Connection, revision_id: int) -> ArticleRevision | None:
        row = connection.execute(
            f"""SELECT {_REVISION_COLUMNS}
               FROM article_revisions AS revisions
               JOIN articles ON articles.id = revisions.article_id
               WHERE revisions.id = ?""",
            (revision_id,),
        ).fetchone()
        return ArticleRevision(*row) if row else None

    def _require_revision(self, connection: sqlite3.Connection, revision_id: int) -> ArticleRevision:
        revision = self._revision(connection, revision_id)
        if revision is None:
            raise ValueError("unknown revision")
        return revision

    def save_claims(self, revision_id: int, claims: Sequence[Claim]) -> list[int]:
        with self._connection() as connection:
            claim_ids = self._save_claims(connection, revision_id, claims)
            self._mark_completed(connection, revision_id, _iso(_moment(None)), None, None, None)
            return claim_ids

    def _save_claims(self, connection: sqlite3.Connection, revision_id: int, claims: Sequence[Claim]) -> list[int]:
        revision = self._require_revision(connection, revision_id)
        claim_ids = []
        for claim in claims:
            if claim.revision_id != revision_id:
                raise ValueError("claim revision_id must match revision_id")
            if not 0 <= claim.start <= claim.end <= len(revision.text):
                raise ValueError("claim span is outside the normalized article text")
            _require_allowed("claim kind", claim.kind, CLAIM_KINDS)
            _require_allowed("claim materiality", claim.materiality, CLAIM_MATERIALITIES)
            _require_allowed("claim extraction_status", claim.extraction_status, CLAIM_EXTRACTION_STATUSES)
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
        return claim_ids

    def save_evidence(self, evidence: Evidence) -> int:
        with self._connection() as connection:
            return self._save_evidence(connection, evidence)

    @staticmethod
    def _save_evidence(connection: sqlite3.Connection, evidence: Evidence) -> int:
        _require_allowed("evidence relation", evidence.relation, _EVIDENCE_RELATIONS)
        _require_allowed("evidence status", evidence.status, _EVIDENCE_STATUSES)
        _require_allowed("evidence source_kind", evidence.source_kind, _EVIDENCE_SOURCE_KINDS)
        retrieved = evidence.status == "retrieved"
        # Page text is only kept for a retrieved page; the assessor's rationale is kept either way.
        title = redact_text(evidence.title, MAX_EVIDENCE_TITLE_CHARS) if retrieved else f"Evidence {evidence.status.replace('_', ' ')}"
        cursor = connection.execute(
            """INSERT INTO evidence (finding_id, url, title, excerpt, relation, status, source_kind, retrieved_at,
                                     provider, published_at, content_hash, rationale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence.finding_id,
                redact_url(evidence.url),
                title,
                redact_text(evidence.excerpt) if retrieved else "",
                evidence.relation,
                evidence.status,
                evidence.source_kind,
                _optional_utc_iso(evidence.retrieved_at),
                _safe_provider_name(evidence.provider),
                _optional_utc_iso(evidence.published_at),
                evidence.content_hash,
                _bounded_text(evidence.rationale, MAX_RATIONALE_CHARS),
            ),
        )
        return cursor.lastrowid

    def save_finding(self, finding: Finding) -> int:
        with self._connection() as connection:
            return self._save_finding(connection, finding)

    def _save_finding(self, connection: sqlite3.Connection, finding: Finding) -> int:
        _require_allowed("finding_type", finding.finding_type, _FINDING_TYPES)
        _require_allowed("finding status", finding.status, _FINDING_STATUSES)
        _require_allowed("finding evidence_status", finding.evidence_status, _EVIDENCE_STATUSES)
        _require_allowed("finding review_status", finding.review_status, REVIEW_STATUSES)
        revision = self._require_revision(connection, finding.revision_id)
        if not 0 <= finding.start <= finding.end <= len(revision.text):
            raise ValueError("finding span is outside the normalized article text")
        if finding.claim_id is not None:
            row = connection.execute("SELECT revision_id FROM claims WHERE id = ?", (finding.claim_id,)).fetchone()
            if row is None or row[0] != finding.revision_id:
                raise ValueError("finding claim must belong to its revision")
        cursor = connection.execute(
            """INSERT INTO findings (revision_id, claim_id, finding_type, summary, start, end, status,
                                     evidence_status, visible, review_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (finding.revision_id, finding.claim_id, finding.finding_type, finding.summary, finding.start, finding.end,
             finding.status, finding.evidence_status, int(finding.visible), finding.review_status),
        )
        return cursor.lastrowid

    def save_analysis(
        self,
        revision_id: int,
        claims: Sequence[Claim],
        findings: Sequence[Finding],
        evidence: Sequence[Sequence[Evidence]],
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        """Persist one completed analysis batch and mark its job completed in a single transaction."""
        finished = _utc_iso(completed_at) if completed_at else _iso(_moment(None))
        with self._connection() as connection:
            claim_ids = self._save_claims(connection, revision_id, claims)
            by_span = {(claim.start, claim.end): claim_id for claim, claim_id in zip(claims, claim_ids)}
            by_provider_id = {claim.id: claim_id for claim, claim_id in zip(claims, claim_ids) if claim.id is not None}
            finding_ids = []
            for finding in findings:
                if finding.revision_id != revision_id:
                    raise ValueError("finding revision_id must match revision_id")
                claim_id = by_provider_id.get(finding.claim_id, by_span.get((finding.start, finding.end)))
                finding_ids.append(self._save_finding(connection, replace(finding, claim_id=claim_id)))
            if len(evidence) != len(finding_ids):
                raise ValueError("evidence batches must match findings")
            for finding_id, batch in zip(finding_ids, evidence):
                for item in batch:
                    self._save_evidence(connection, replace(item, finding_id=finding_id))
            self._mark_completed(connection, revision_id, finished, provider, model, prompt_version)

    # --- analysis job state machine ------------------------------------------

    @staticmethod
    def _mark_completed(
        connection: sqlite3.Connection,
        revision_id: int,
        completed_at: str,
        provider: str | None,
        model: str | None,
        prompt_version: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO revision_analysis (revision_id, status, attempts, last_error, updated_at, provider, model, prompt_version)
               VALUES (?, 'completed', 0, NULL, ?, ?, ?, ?)
               ON CONFLICT(revision_id) DO UPDATE SET
                 status = 'completed',
                 last_error = NULL,
                 updated_at = excluded.updated_at,
                 provider = COALESCE(excluded.provider, revision_analysis.provider),
                 model = COALESCE(excluded.model, revision_analysis.model),
                 prompt_version = COALESCE(excluded.prompt_version, revision_analysis.prompt_version)""",
            (revision_id, completed_at, _safe_provider_name(provider), _safe_provider_name(model),
             _safe_provider_name(prompt_version)),
        )

    def mark_analysis_running(self, revision_id: int, at: str) -> bool:
        """Claim a revision's job; re-claiming an abandoned running job counts it as one failed attempt.

        Returns False (and changes nothing) when the analysis is already completed.
        """
        started = _utc_iso(at)
        with self._connection() as connection:
            self._require_revision(connection, revision_id)
            cursor = connection.execute(
                """INSERT INTO revision_analysis (revision_id, status, attempts, updated_at) VALUES (?, 'running', 0, ?)
                   ON CONFLICT(revision_id) DO UPDATE SET
                     status = 'running',
                     attempts = revision_analysis.attempts + (revision_analysis.status = 'running'),
                     last_error = CASE WHEN revision_analysis.status = 'running'
                                       THEN 'previous analysis run did not finish'
                                       ELSE revision_analysis.last_error END,
                     updated_at = excluded.updated_at
                   WHERE revision_analysis.status != 'completed'""",
                (revision_id, started),
            )
            return cursor.rowcount == 1

    def mark_analysis_failed(self, revision_id: int, at: str, error: str | None) -> bool:
        """Record one failed attempt with a redacted, bounded error; False when already completed."""
        failed = _utc_iso(at)
        with self._connection() as connection:
            self._require_revision(connection, revision_id)
            cursor = connection.execute(
                """INSERT INTO revision_analysis (revision_id, status, attempts, last_error, updated_at)
                   VALUES (?, 'failed', 1, ?, ?)
                   ON CONFLICT(revision_id) DO UPDATE SET
                     status = 'failed',
                     attempts = revision_analysis.attempts + 1,
                     last_error = excluded.last_error,
                     updated_at = excluded.updated_at
                   WHERE revision_analysis.status != 'completed'""",
                (revision_id, _error_text(error), failed),
            )
            return cursor.rowcount == 1

    def mark_analysis_skipped(self, revision_id: int, at: str, reason: str | None) -> bool:
        """Defer a job without counting an attempt (e.g. budget reached); False when already completed."""
        skipped = _utc_iso(at)
        with self._connection() as connection:
            self._require_revision(connection, revision_id)
            cursor = connection.execute(
                """INSERT INTO revision_analysis (revision_id, status, attempts, last_error, updated_at)
                   VALUES (?, 'skipped', 0, ?, ?)
                   ON CONFLICT(revision_id) DO UPDATE SET
                     status = 'skipped',
                     last_error = excluded.last_error,
                     updated_at = excluded.updated_at
                   WHERE revision_analysis.status != 'completed'""",
                (revision_id, _error_text(reason), skipped),
            )
            return cursor.rowcount == 1

    def analysis_status(self, revision_id: int) -> dict[str, object] | None:
        with self._connection(write=False) as connection:
            row = connection.execute(
                f"SELECT {', '.join(_ANALYSIS_STATUS_KEYS)} FROM revision_analysis WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
        return dict(zip(_ANALYSIS_STATUS_KEYS, row)) if row else None

    def analysis_counts(self) -> dict[str, int]:
        """Count analysis states of each article's current revision; 'unanalyzed' means no job row yet."""
        with self._connection(write=False) as connection:
            return self._analysis_counts(connection)

    @staticmethod
    def _analysis_counts(connection: sqlite3.Connection) -> dict[str, int]:
        counts = {status: 0 for status in sorted(ANALYSIS_STATUSES)}
        counts["unanalyzed"] = 0
        rows = connection.execute(
            """SELECT COALESCE(analysis.status, 'unanalyzed'), COUNT(*)
               FROM articles
               LEFT JOIN revision_analysis AS analysis ON analysis.revision_id = articles.current_revision_id
               WHERE articles.current_revision_id IS NOT NULL
               GROUP BY 1"""
        )
        for status, count in rows:
            counts[status] = count
        return counts

    def list_pending_revisions(
        self,
        limit: int = 20,
        *,
        max_attempts: int = 3,
        now: str | datetime | None = None,
        stale_after_minutes: int = 60,
    ) -> list[ArticleRevision]:
        """Return current revisions whose analysis is due, ordered by revision id.

        Due means: no job row, pending, skipped, failed fewer than max_attempts times, or
        running since before now - stale_after_minutes (a crashed run). Superseded
        revisions are never returned.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if stale_after_minutes < 1:
            raise ValueError("stale_after_minutes must be positive")
        cutoff = _iso(_moment(now) - timedelta(minutes=stale_after_minutes))
        with self._connection(write=False) as connection:
            rows = connection.execute(
                f"""SELECT {_REVISION_COLUMNS}
                   FROM articles
                   JOIN article_revisions AS revisions ON revisions.id = articles.current_revision_id
                   LEFT JOIN revision_analysis AS analysis ON analysis.revision_id = revisions.id
                   WHERE analysis.revision_id IS NULL
                      OR analysis.status IN ('pending', 'skipped')
                      OR (analysis.status = 'failed' AND analysis.attempts < :max_attempts)
                      OR (analysis.status = 'running' AND analysis.attempts < :max_attempts
                          AND COALESCE(julianday(analysis.updated_at) < julianday(:cutoff), 1))
                   ORDER BY revisions.id LIMIT :limit""",
                {"max_attempts": max_attempts, "cutoff": cutoff, "limit": limit},
            )
            return [ArticleRevision(*row) for row in rows]

    def list_recent_revisions(self, limit: int = 100, *, current_only: bool = False) -> list[ArticleRevision]:
        """Return a bounded local context pool; callers must apply topic rules.

        current_only keeps just each article's current revision, so superseded text never acts as a peer.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        current = "WHERE articles.current_revision_id = revisions.id" if current_only else ""
        with self._connection(write=False) as connection:
            return [ArticleRevision(*row) for row in connection.execute(
                f"""SELECT {_REVISION_COLUMNS}
                   FROM article_revisions AS revisions
                   JOIN articles ON articles.id = revisions.article_id
                   {current}
                   ORDER BY revisions.fetched_at DESC, revisions.id DESC LIMIT ?""",
                (limit,),
            )]

    # --- LLM call ledger ------------------------------------------------------

    def record_llm_call(
        self,
        called_at: str,
        provider: str | None,
        model: str | None,
        purpose: str,
        revision_id: int | None,
        status: str,
        input_chars: int = 0,
        output_chars: int = 0,
    ) -> int:
        """Record one provider call for cost control; stores sizes and labels, never prompts or keys."""
        called = _utc_iso(called_at)
        _required_label("llm call purpose", purpose)
        _required_label("llm call status", status)
        if input_chars < 0 or output_chars < 0:
            raise ValueError("llm call sizes must not be negative")
        with self._connection() as connection:
            if revision_id is not None:
                self._require_revision(connection, revision_id)
            cursor = connection.execute(
                """INSERT INTO llm_calls (called_at, provider, model, purpose, revision_id, status, input_chars, output_chars)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (called, _safe_provider_name(provider), _safe_provider_name(model), purpose, revision_id, status,
                 int(input_chars), int(output_chars)),
            )
            return cursor.lastrowid

    def count_llm_calls_since(self, since_iso: str) -> int:
        since = _sortable(since_iso)
        with self._connection(write=False) as connection:
            # The 19-char second prefix sorts before every stored value of that second, so the
            # indexed range scan is a safe lower bound and the exact comparison does the rest.
            return connection.execute(
                f"SELECT COUNT(*) FROM llm_calls WHERE called_at >= ? AND {_sortable_sql('called_at')} >= ?",
                (since[:19], since),
            ).fetchone()[0]

    # --- same-topic grouping ---------------------------------------------------

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

    def list_topic_revisions(self, topic_id: int) -> list[ArticleRevision]:
        with self._connection(write=False) as connection:
            return [ArticleRevision(*row) for row in connection.execute(
                f"""SELECT {_REVISION_COLUMNS}
                   FROM topic_articles
                   JOIN article_revisions AS revisions ON revisions.id = topic_articles.revision_id
                   JOIN articles ON articles.id = revisions.article_id
                   WHERE topic_articles.topic_id = ? ORDER BY revisions.id""",
                (topic_id,),
            )]

    @staticmethod
    def _topic_for_revision(connection: sqlite3.Connection, revision_id: int) -> int | None:
        row = connection.execute(
            """SELECT topics.id FROM topic_articles
               JOIN topics ON topics.id = topic_articles.topic_id
               WHERE topic_articles.revision_id = ? AND topics.status != 'dismissed'
               ORDER BY CASE topics.status WHEN 'active' THEN 0 ELSE 1 END, topics.id
               LIMIT 1""",
            (revision_id,),
        ).fetchone()
        return row[0] if row else None

    def topic_id_for_revision(self, revision_id: int) -> int | None:
        """Return a non-dismissed topic linked to the revision, preferring active topics."""
        with self._connection(write=False) as connection:
            return self._topic_for_revision(connection, revision_id)

    def assign_topic(self, revision_id: int, peer_revision_id: int, label: str, confidence: str) -> int:
        """Group a revision with a peer, joining the peer's (or the revision's) open topic when one exists.

        A 'high' signal upgrades a joined topic to high confidence (possible -> active); nothing is
        ever downgraded. When a reviewer already dismissed a topic linking exactly this pair, that
        topic's id is returned unchanged so the machine cannot re-open a human decision.
        """
        _require_allowed("topic confidence", confidence, _TOPIC_CONFIDENCES)
        if revision_id == peer_revision_id:
            raise ValueError("a revision cannot be its own topic peer")
        safe_label = _bounded_text(label, MAX_TOPIC_LABEL_CHARS)
        if safe_label is None:
            raise ValueError("topic label must not be empty")
        with self._connection() as connection:
            self._require_revision(connection, revision_id)
            self._require_revision(connection, peer_revision_id)
            dismissed = connection.execute(
                """SELECT topics.id FROM topics
                   JOIN topic_articles AS own ON own.topic_id = topics.id AND own.revision_id = ?
                   JOIN topic_articles AS peer ON peer.topic_id = topics.id AND peer.revision_id = ?
                   WHERE topics.status = 'dismissed' ORDER BY topics.id LIMIT 1""",
                (revision_id, peer_revision_id),
            ).fetchone()
            if dismissed is not None:
                return dismissed[0]
            topic_id = self._topic_for_revision(connection, peer_revision_id)
            if topic_id is None:
                topic_id = self._topic_for_revision(connection, revision_id)
            if topic_id is None:
                cursor = connection.execute(
                    "INSERT INTO topics (label, confidence, status) VALUES (?, ?, ?)",
                    (safe_label, confidence, "active" if confidence == "high" else "possible"),
                )
                topic_id = cursor.lastrowid
            elif confidence == "high":
                connection.execute(
                    """UPDATE topics SET confidence = 'high',
                         status = CASE WHEN status = 'possible' THEN 'active' ELSE status END
                       WHERE id = ?""",
                    (topic_id,),
                )
            connection.executemany(
                "INSERT OR IGNORE INTO topic_articles (topic_id, revision_id) VALUES (?, ?)",
                [(topic_id, revision_id), (topic_id, peer_revision_id)],
            )
            return topic_id

    def topic_status(self, topic_id: int) -> str | None:
        """Return a topic's status ('active', 'possible' or 'dismissed'), or None when it does not exist."""
        with self._connection(write=False) as connection:
            row = connection.execute("SELECT status FROM topics WHERE id = ?", (topic_id,)).fetchone()
        return row[0] if row else None

    def set_topic_status(self, topic_id: int, status: str) -> bool:
        _require_allowed("topic status", status, _TOPIC_STATUSES)
        with self._connection() as connection:
            cursor = connection.execute("UPDATE topics SET status = ? WHERE id = ?", (status, topic_id))
            return cursor.rowcount == 1

    # --- human review ----------------------------------------------------------

    def review_finding(self, finding_id: int, review_status: str, reviewed_at: str, note: str | None = None) -> bool:
        """Record a reviewer's decision; independent of the machine-side status and visibility."""
        _require_allowed("review_status", review_status, REVIEW_STATUSES)
        reviewed = _utc_iso(reviewed_at)
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE findings SET review_status = ?, reviewed_at = ?, review_note = ? WHERE id = ?",
                (review_status, reviewed, _bounded_text(note, MAX_REVIEW_NOTE_CHARS), finding_id),
            )
            return cursor.rowcount == 1

    # --- read models for the local web page ------------------------------------

    @staticmethod
    def _evidence_for(connection: sqlite3.Connection, finding_ids: Sequence[int]) -> dict[int, list[dict[str, object]]]:
        grouped: dict[int, list[dict[str, object]]] = {finding_id: [] for finding_id in finding_ids}
        ids = list(grouped)
        for chunk in _chunks(ids):
            rows = connection.execute(
                f"""SELECT finding_id, {', '.join(_EVIDENCE_KEYS)} FROM evidence
                   WHERE finding_id IN ({_placeholders(chunk)}) ORDER BY id""",
                tuple(chunk),
            )
            for finding_id, *values in rows:
                item = dict(zip(_EVIDENCE_KEYS, values))
                item["url"] = _display_url(item["url"])
                grouped[finding_id].append(item)
        return grouped

    def list_findings(self, limit: int = 50, *, visible_only: bool = True) -> list[dict[str, object]]:
        """Return findings newest first with their evidence chain; URLs are redacted."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connection(write=False) as connection:
            return self._list_findings(connection, limit, visible_only)

    def _list_findings(self, connection: sqlite3.Connection, limit: int, visible_only: bool) -> list[dict[str, object]]:
        rows = connection.execute(
            """SELECT findings.id, findings.revision_id, revisions.article_id, revisions.title, articles.url, sources.name,
                      findings.finding_type, findings.summary, findings.start, findings.end, findings.status,
                      findings.evidence_status, findings.visible, findings.review_status, findings.reviewed_at
               FROM findings
               JOIN article_revisions AS revisions ON revisions.id = findings.revision_id
               JOIN articles ON articles.id = revisions.article_id
               JOIN sources ON sources.id = articles.source_id
               WHERE findings.visible = 1 OR NOT ?
               ORDER BY findings.id DESC LIMIT ?""",
            (int(visible_only), limit),
        ).fetchall()
        evidence = self._evidence_for(connection, [row[0] for row in rows])
        return [
            {
                "id": finding_id,
                "revision_id": revision_id,
                "article_id": article_id,
                "article_title": article_title,
                "article_url": _display_url(article_url),
                "source": source,
                "finding_type": finding_type,
                "summary": summary,
                "start": start,
                "end": end,
                "status": status,
                "evidence_status": evidence_status,
                "visible": bool(visible),
                "review_status": review_status,
                "reviewed_at": reviewed_at,
                "evidence": evidence[finding_id],
            }
            for (finding_id, revision_id, article_id, article_title, article_url, source, finding_type, summary,
                 start, end, status, evidence_status, visible, review_status, reviewed_at) in rows
        ]

    def _inbox(self, connection: sqlite3.Connection, limit: int) -> list[dict[str, object]]:
        rows = connection.execute(
            """SELECT articles.id, revisions.id, revisions.title, articles.url, sources.name, articles.published_at,
                      revisions.fetched_at, revisions.text, COALESCE(analysis.status, 'unanalyzed')
               FROM articles
               JOIN article_revisions AS revisions ON revisions.id = articles.current_revision_id
               JOIN sources ON sources.id = articles.source_id
               LEFT JOIN revision_analysis AS analysis ON analysis.revision_id = revisions.id
               ORDER BY revisions.fetched_at DESC, revisions.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        revision_ids = [row[1] for row in rows]
        annotations: dict[int, list[Finding]] = {revision_id: [] for revision_id in revision_ids}
        pending: dict[int, int] = {revision_id: 0 for revision_id in revision_ids}
        for chunk in _chunks(revision_ids):
            findings = connection.execute(
                f"""SELECT id, revision_id, claim_id, finding_type, summary, start, end, status, evidence_status,
                          visible, review_status
                   FROM findings WHERE revision_id IN ({_placeholders(chunk)}) ORDER BY start, end, id""",
                tuple(chunk),
            )
            for (finding_id, revision_id, claim_id, finding_type, summary, start, end, status, evidence_status,
                 visible, review_status) in findings:
                if visible:
                    annotations[revision_id].append(Finding(
                        finding_id, revision_id, claim_id, finding_type, summary, start, end, status,
                        evidence_status, True, review_status,
                    ))
                elif status == "pending":
                    pending[revision_id] += 1
        evidence = self._evidence_for(
            connection, [finding.id for items in annotations.values() for finding in items if finding.id is not None]
        )
        return [
            {
                "article_id": article_id,
                "revision_id": revision_id,
                "title": title,
                "url": _display_url(url),
                "source": source,
                "published_at": published_at,
                "updated_at": fetched_at,
                "text": text,
                "analysis_status": analysis_status,
                "annotations": tuple(annotations[revision_id]),
                "evidence": [item for finding in annotations[revision_id] for item in evidence[finding.id]],
                "pending_findings": pending[revision_id],
            }
            for article_id, revision_id, title, url, source, published_at, fetched_at, text, analysis_status in rows
        ]

    @staticmethod
    def _comparisons(connection: sqlite3.Connection, limit: int) -> list[dict[str, object]]:
        topics = connection.execute(
            """SELECT topics.id, topics.label, topics.confidence, topics.status
               FROM topics
               WHERE topics.status != 'dismissed'
                 AND (SELECT COUNT(DISTINCT revisions.article_id)
                      FROM topic_articles
                      JOIN article_revisions AS revisions ON revisions.id = topic_articles.revision_id
                      WHERE topic_articles.topic_id = topics.id) >= 2
               ORDER BY topics.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        topic_ids = [row[0] for row in topics]
        # One entry per article: its newest revision linked to the topic.
        latest: dict[int, dict[int, dict[str, object]]] = {topic_id: {} for topic_id in topic_ids}
        for chunk in _chunks(topic_ids):
            rows = connection.execute(
                f"""SELECT topic_articles.topic_id, revisions.article_id, revisions.id, revisions.title,
                          articles.url, sources.name
                   FROM topic_articles
                   JOIN article_revisions AS revisions ON revisions.id = topic_articles.revision_id
                   JOIN articles ON articles.id = revisions.article_id
                   JOIN sources ON sources.id = articles.source_id
                   WHERE topic_articles.topic_id IN ({_placeholders(chunk)})
                   ORDER BY revisions.id""",
                tuple(chunk),
            )
            for topic_id, article_id, revision_id, title, url, source in rows:
                latest[topic_id][article_id] = {
                    "title": title, "url": _display_url(url), "source": source, "revision_id": revision_id,
                }
        return [
            {
                "id": topic_id,
                "label": label,
                "confidence": confidence,
                "status": status,
                "articles": sorted(latest[topic_id].values(), key=lambda item: item["revision_id"]),
            }
            for topic_id, label, confidence, status in topics
        ]

    @staticmethod
    def _source_health(connection: sqlite3.Connection) -> list[dict[str, object]]:
        rows = connection.execute(
            """SELECT sources.id, sources.name, sources.enabled, checks.status, checks.checked_at,
                      checks.candidates_seen, checks.error
               FROM sources
               LEFT JOIN source_checks AS checks ON checks.id = (
                   SELECT id FROM source_checks WHERE source_id = sources.id
                   ORDER BY checked_at DESC, id DESC LIMIT 1)
               ORDER BY sources.id"""
        )
        return [
            {
                "id": source_id,
                "name": name,
                "enabled": bool(enabled),
                "status": status if status is not None else "not checked",
                "checked_at": checked_at,
                "candidates_seen": candidates_seen,
                "error": _safe_error(error),
            }
            for source_id, name, enabled, status, checked_at, candidates_seen, error in rows
        ]

    @staticmethod
    def _last_run(connection: sqlite3.Connection) -> dict[str, object] | None:
        row = connection.execute(f"SELECT {', '.join(_RUN_KEYS)} FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(zip(_RUN_KEYS, row)) if row else None

    def dashboard_snapshot(self, limit: int = 50) -> dict[str, object]:
        """Return one consistent read snapshot of everything the local page renders."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connection(write=False) as connection:
            return {
                "inbox": self._inbox(connection, limit),
                "findings": self._list_findings(connection, limit, True),
                "comparisons": self._comparisons(connection, limit),
                "sources": self._source_health(connection),
                "last_run": self._last_run(connection),
                "analysis": self._analysis_counts(connection),
            }
