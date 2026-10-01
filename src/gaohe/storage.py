"""Local SQLite store: sources, article revisions, analysis results and read models for the page.

One schema, no migration history: GaoHe has not been released, so an incompatible database
is reported with a plain message instead of being upgraded. Free text and URLs are redacted
on the way in; API keys never reach this file.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .domain import (
    ArticleCandidate,
    ArticleRevision,
    Claim,
    Evidence,
    FetchedArticle,
    Finding,
    RunSummary,
    Source,
    article_content_hash,
    normalize_article_content,
)
from .safety import redact_text, redact_url, safe_error


SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
MANUAL_SOURCE_NAME = "單篇查核"
MANUAL_SOURCE_FEED_URL = "gaohe:manual"
INCOMPATIBLE_MESSAGE = "本機資料庫格式不相容（可能來自舊版測試）。請備份後刪除資料夾中的 gaohe.db，再重新執行。"

_SCHEMA = """
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    feed_url TEXT NOT NULL UNIQUE,
    article_url TEXT,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    kind TEXT NOT NULL DEFAULT 'feed' CHECK (kind IN ('feed', 'manual'))
);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    current_revision_id INTEGER REFERENCES article_revisions(id),
    last_fetched_at TEXT
);
CREATE TABLE article_revisions (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE TABLE analysis_jobs (
    revision_id INTEGER PRIMARY KEY REFERENCES article_revisions(id),
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT
);
CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES article_revisions(id),
    text TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    kind TEXT NOT NULL,
    materiality TEXT NOT NULL,
    UNIQUE (revision_id, start, end)
);
CREATE TABLE findings (
    id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES article_revisions(id),
    finding_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    evidence_status TEXT NOT NULL,
    visible INTEGER NOT NULL CHECK (visible IN (0, 1))
);
CREATE TABLE evidence (
    id INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES findings(id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    relation TEXT NOT NULL,
    status TEXT NOT NULL,
    retrieved_at TEXT,
    provider TEXT,
    rationale TEXT
);
CREATE TABLE source_checks (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    candidates_seen INTEGER NOT NULL,
    error TEXT
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    sources_checked INTEGER NOT NULL,
    candidates_seen INTEGER NOT NULL,
    revisions_created INTEGER NOT NULL,
    failures INTEGER NOT NULL
);
CREATE INDEX idx_revisions_article ON article_revisions(article_id);
CREATE INDEX idx_findings_revision ON findings(revision_id);
CREATE INDEX idx_evidence_finding ON evidence(finding_id);
CREATE INDEX idx_source_checks_source ON source_checks(source_id, checked_at);
"""

_REVISION_COLUMNS = """revisions.id, revisions.article_id, articles.url, revisions.title,
    revisions.text, revisions.content_hash, revisions.fetched_at"""


class IncompatibleDatabase(sqlite3.DatabaseError):
    """The file was written by a different GaoHe schema."""


def _utc_iso(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware ISO-8601 strings")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _evidence_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in ("url", "title", "excerpt", "relation", "status", "retrieved_at", "provider", "rationale")}


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """One transaction per call: IMMEDIATE for writes, a consistent snapshot for reads."""
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            has_tables = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
            if version == SCHEMA_VERSION:
                return
            if version != 0 or has_tables:
                raise IncompatibleDatabase(INCOMPATIBLE_MESSAGE)
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # The scheduled watch and the local page may read and write at the same time.
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass  # another process holds the file; busy_timeout still serializes access
        finally:
            connection.close()

    # --- sources ---------------------------------------------------------------------------

    def add_source(self, source: Source) -> int:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO sources (name, feed_url, article_url, enabled) VALUES (?, ?, ?, ?)
                   ON CONFLICT(feed_url) DO UPDATE SET
                     name = excluded.name, article_url = excluded.article_url, enabled = excluded.enabled""",
                (source.name, source.feed_url, source.article_url, int(source.enabled)),
            )
            return connection.execute("SELECT id FROM sources WHERE feed_url = ?", (source.feed_url,)).fetchone()[0]

    def list_sources(self, enabled_only: bool = False, *, include_manual: bool = False) -> list[Source]:
        conditions = (["enabled = 1"] if enabled_only else []) + ([] if include_manual else ["kind = 'feed'"])
        query = "SELECT id, name, feed_url, article_url, enabled, kind FROM sources"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        with self._connection(write=False) as connection:
            return [Source(row[0], row[1], row[2], row[3], bool(row[4]), row[5]) for row in connection.execute(query + " ORDER BY id")]

    def set_source_enabled(self, source_id: int, enabled: bool) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE sources SET enabled = ? WHERE id = ? AND kind = 'feed'", (int(enabled), source_id)
            )
            return cursor.rowcount == 1

    def ensure_manual_source(self) -> int:
        """The never-polled pseudo-source that owns single articles checked on demand."""
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO sources (name, feed_url, article_url, enabled, kind)
                   VALUES (?, ?, NULL, 0, 'manual') ON CONFLICT(feed_url) DO NOTHING""",
                (MANUAL_SOURCE_NAME, MANUAL_SOURCE_FEED_URL),
            )
            return connection.execute("SELECT id FROM sources WHERE feed_url = ?", (MANUAL_SOURCE_FEED_URL,)).fetchone()[0]

    # --- articles --------------------------------------------------------------------------

    @staticmethod
    def _upsert_article(connection: sqlite3.Connection, candidate: ArticleCandidate) -> sqlite3.Row:
        # First-seen time and the discovering source are facts; a later listing only refreshes metadata.
        connection.execute(
            """INSERT INTO articles (source_id, url, title, published_at, discovered_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET title = excluded.title,
                 published_at = COALESCE(excluded.published_at, articles.published_at),
                 metadata_json = excluded.metadata_json""",
            (
                candidate.source_id, candidate.url, candidate.title,
                _utc_iso(candidate.published_at) if candidate.published_at else None,
                _utc_iso(candidate.discovered_at), json.dumps(candidate.metadata, sort_keys=True, ensure_ascii=False),
            ),
        )
        return connection.execute("SELECT id, current_revision_id FROM articles WHERE url = ?", (candidate.url,)).fetchone()

    def save_candidate(self, candidate: ArticleCandidate) -> int:
        """Record an article seen in a listing before its page is fetched (metadata first, spec 5)."""
        with self._connection() as connection:
            return self._upsert_article(connection, candidate)["id"]

    def save_fetched_article(self, article: FetchedArticle) -> tuple[int, bool]:
        """Store fetched text; return (current revision id, whether a new revision was created)."""
        if article.content_hash != article_content_hash(article.candidate.title, article.text):
            raise ValueError("content_hash must match normalized title and text")
        title, text = normalize_article_content(article.candidate.title, article.text)
        fetched_at = _utc_iso(article.fetched_at)
        with self._connection() as connection:
            row = self._upsert_article(connection, replace(article.candidate, title=title))
            connection.execute("UPDATE articles SET last_fetched_at = ? WHERE id = ?", (fetched_at, row["id"]))
            if row["current_revision_id"] is not None:
                current = connection.execute(
                    "SELECT content_hash FROM article_revisions WHERE id = ?", (row["current_revision_id"],)
                ).fetchone()
                if current["content_hash"] == article.content_hash:
                    return row["current_revision_id"], False
            cursor = connection.execute(
                "INSERT INTO article_revisions (article_id, title, text, fetched_at, content_hash) VALUES (?, ?, ?, ?, ?)",
                (row["id"], title, text, fetched_at, article.content_hash),
            )
            connection.execute("UPDATE articles SET current_revision_id = ? WHERE id = ?", (cursor.lastrowid, row["id"]))
            return cursor.lastrowid, True

    def article_state(self, url: str) -> dict[str, object] | None:
        """What the monitor needs to decide whether to fetch a listed article again."""
        with self._connection(write=False) as connection:
            row = connection.execute(
                """SELECT articles.published_at, articles.discovered_at, articles.last_fetched_at,
                          revisions.content_hash
                   FROM articles LEFT JOIN article_revisions AS revisions ON revisions.id = articles.current_revision_id
                   WHERE articles.url = ?""",
                (url,),
            ).fetchone()
            return dict(row) if row else None

    def record_source_check(self, source_id: int, checked_at: str, status: str, candidates_seen: int, error: str | None) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO source_checks (source_id, checked_at, status, candidates_seen, error) VALUES (?, ?, ?, ?, ?)",
                (source_id, _utc_iso(checked_at), status, candidates_seen, safe_error(error)),
            )

    def record_run(self, summary: RunSummary) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO runs (started_at, finished_at, sources_checked, candidates_seen, revisions_created, failures)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_utc_iso(summary.started_at), _utc_iso(summary.finished_at), summary.sources_checked,
                 summary.candidates_seen, summary.revisions_created, summary.failures),
            )

    # --- analysis jobs ---------------------------------------------------------------------

    def list_pending_revisions(
        self, limit: int = 20, *, max_attempts: int = 3, revision_ids: Sequence[int] | None = None
    ) -> list[ArticleRevision]:
        """Current revisions with no finished analysis, oldest first; failed ones retry up to max_attempts."""
        if limit < 1:
            raise ValueError("limit must be positive")
        query = f"""SELECT {_REVISION_COLUMNS} FROM article_revisions AS revisions
            JOIN articles ON articles.current_revision_id = revisions.id
            LEFT JOIN analysis_jobs AS jobs ON jobs.revision_id = revisions.id
            WHERE (jobs.revision_id IS NULL OR (jobs.status = 'failed' AND jobs.attempts < ?))"""
        parameters: list[object] = [max_attempts]
        if revision_ids is not None:
            ids = [int(value) for value in revision_ids]
            if not ids:
                return []
            query += f" AND revisions.id IN ({', '.join('?' for _ in ids)})"
            parameters += ids
        query += " ORDER BY revisions.id LIMIT ?"
        with self._connection(write=False) as connection:
            return [ArticleRevision(*row) for row in connection.execute(query, (*parameters, limit))]

    def mark_analysis_failed(self, revision_id: int, at: str, error: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO analysis_jobs (revision_id, status, attempts, last_error, updated_at)
                   VALUES (?, 'failed', 1, ?, ?)
                   ON CONFLICT(revision_id) DO UPDATE SET status = 'failed', attempts = analysis_jobs.attempts + 1,
                     last_error = excluded.last_error, updated_at = excluded.updated_at
                   WHERE analysis_jobs.status = 'failed'""",
                (revision_id, safe_error(error), _utc_iso(at)),
            )

    def reset_analysis(self, revision_id: int) -> None:
        """Forget a failed attempt so an explicit re-check runs it again."""
        with self._connection() as connection:
            connection.execute("DELETE FROM analysis_jobs WHERE revision_id = ? AND status = 'failed'", (revision_id,))

    def analysis_status(self, revision_id: int) -> dict[str, object] | None:
        with self._connection(write=False) as connection:
            row = connection.execute(
                "SELECT status, attempts, last_error, updated_at, model, prompt_version FROM analysis_jobs WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            return dict(row) if row else None

    def save_analysis(
        self,
        revision_id: int,
        claims: Sequence[Claim],
        findings: Sequence[Finding],
        evidence: Sequence[Sequence[Evidence]],
        *,
        completed_at: str,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> bool:
        """Persist one analysis in a single transaction; False when another run already completed it."""
        if len(evidence) != len(findings):
            raise ValueError("evidence batches must match findings")
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_REVISION_COLUMNS} FROM article_revisions AS revisions "
                "JOIN articles ON articles.id = revisions.article_id WHERE revisions.id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown revision")
            text = row["text"]
            done = connection.execute(
                "SELECT 1 FROM analysis_jobs WHERE revision_id = ? AND status = 'completed'", (revision_id,)
            ).fetchone()
            if done:
                return False  # two overlapping runs analyzed the same revision; keep the first result only
            for claim in claims:
                if claim.revision_id != revision_id or text[claim.start:claim.end] != claim.text:
                    raise ValueError("claim does not belong to this revision text")
                connection.execute(
                    "INSERT OR IGNORE INTO claims (revision_id, text, start, end, kind, materiality) VALUES (?, ?, ?, ?, ?, ?)",
                    (revision_id, claim.text, claim.start, claim.end, claim.kind, claim.materiality),
                )
            for finding, batch in zip(findings, evidence):
                if finding.revision_id != revision_id or not 0 <= finding.start < finding.end <= len(text):
                    raise ValueError("finding span is outside the revision text")
                cursor = connection.execute(
                    """INSERT INTO findings (revision_id, finding_type, summary, start, end, evidence_status, visible)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (revision_id, finding.finding_type, redact_text(finding.summary), finding.start, finding.end,
                     finding.evidence_status, int(finding.visible)),
                )
                for item in batch:
                    retrieved = item.status == "retrieved"
                    connection.execute(
                        """INSERT INTO evidence (finding_id, url, title, excerpt, relation, status, retrieved_at, provider, rationale)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (cursor.lastrowid, redact_url(item.url), redact_text(item.title, 500),
                         redact_text(item.excerpt) if retrieved else "", item.relation, item.status,
                         _utc_iso(item.retrieved_at) if item.retrieved_at else None,
                         redact_text(item.provider, 64) or None, redact_text(item.rationale, 1000) or None),
                    )
            connection.execute(
                """INSERT INTO analysis_jobs (revision_id, status, attempts, updated_at, model, prompt_version)
                   VALUES (?, 'completed', 0, ?, ?, ?)
                   ON CONFLICT(revision_id) DO UPDATE SET status = 'completed', last_error = NULL,
                     updated_at = excluded.updated_at, model = excluded.model, prompt_version = excluded.prompt_version""",
                (revision_id, _utc_iso(completed_at), redact_text(model, 64) or None, prompt_version),
            )
            return True

    # --- read models -----------------------------------------------------------------------

    def finding_counts(self, revision_id: int) -> tuple[int, int]:
        """(visible, pending) findings of one revision."""
        with self._connection(write=False) as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(visible), 0), COUNT(*) - COALESCE(SUM(visible), 0) FROM findings WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            return int(row[0]), int(row[1])

    def dashboard_snapshot(self, limit: int = 50) -> dict[str, object]:
        """Everything the local page shows, from one consistent read."""
        with self._connection(write=False) as connection:
            revisions = connection.execute(
                f"""SELECT {_REVISION_COLUMNS}, sources.name AS source, sources.kind AS source_kind, jobs.status AS job_status
                    FROM articles
                    JOIN article_revisions AS revisions ON revisions.id = articles.current_revision_id
                    JOIN sources ON sources.id = articles.source_id
                    LEFT JOIN analysis_jobs AS jobs ON jobs.revision_id = revisions.id
                    ORDER BY revisions.fetched_at DESC, revisions.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            ids = [row["id"] for row in revisions]
            marks = ", ".join("?" for _ in ids) or "NULL"
            finding_rows = connection.execute(
                f"SELECT * FROM findings WHERE revision_id IN ({marks}) ORDER BY start, id", ids
            ).fetchall()
            finding_ids = [row["id"] for row in finding_rows]
            evidence_marks = ", ".join("?" for _ in finding_ids) or "NULL"
            evidence: dict[int, list[dict[str, object]]] = {}
            for row in connection.execute(
                f"SELECT * FROM evidence WHERE finding_id IN ({evidence_marks}) ORDER BY id", finding_ids
            ):
                evidence.setdefault(row["finding_id"], []).append(_evidence_dict(row))
            sources = connection.execute(
                """SELECT sources.id, sources.name, sources.enabled, checks.status, checks.checked_at,
                          checks.candidates_seen, checks.error
                   FROM sources LEFT JOIN source_checks AS checks ON checks.id = (
                       SELECT id FROM source_checks WHERE source_id = sources.id ORDER BY checked_at DESC, id DESC LIMIT 1)
                   WHERE sources.kind = 'feed' ORDER BY sources.id"""
            ).fetchall()
            last_run = connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

        by_revision: dict[int, list[sqlite3.Row]] = {}
        for row in finding_rows:
            by_revision.setdefault(row["revision_id"], []).append(row)
        inbox, findings = [], []
        for revision in revisions:
            visible = [row for row in by_revision.get(revision["id"], []) if row["visible"]]
            annotations = [
                Finding(row["id"], row["revision_id"], row["finding_type"], row["summary"], row["start"], row["end"],
                        row["evidence_status"], True)
                for row in visible
            ]
            inbox.append({
                "revision_id": revision["id"],
                "title": revision["title"],
                "url": redact_url(revision["url"]),
                "source": revision["source"],
                "manual": revision["source_kind"] == "manual",
                "updated_at": revision["fetched_at"],
                "text": revision["text"],
                "analysis_status": revision["job_status"] or "pending",
                "annotations": annotations,
                "evidence": [item for row in visible for item in evidence.get(row["id"], [])],
                "pending_findings": len(by_revision.get(revision["id"], [])) - len(visible),
            })
            for row in visible:
                findings.append({
                    "id": row["id"], "revision_id": revision["id"], "finding_type": row["finding_type"],
                    "summary": row["summary"], "evidence_status": row["evidence_status"],
                    "article_title": revision["title"], "article_url": redact_url(revision["url"]),
                    "evidence": evidence.get(row["id"], []),
                })
        return {
            "inbox": inbox,
            "findings": findings,
            "sources": [
                {"id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]),
                 "status": row["status"] or "not checked", "checked_at": row["checked_at"],
                 "candidates_seen": row["candidates_seen"] or 0, "error": row["error"]}
                for row in sources
            ],
            "last_run": dict(last_run) if last_run else None,
        }
