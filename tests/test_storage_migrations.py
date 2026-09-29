from pathlib import Path
import sqlite3

import pytest

import gaohe.storage as storage
from gaohe.domain import Claim, Evidence, Finding, Source, TopicGroup, article_content_hash
from gaohe.storage import SCHEMA_VERSION, SchemaVersionError, Store

from test_storage import candidate, fetched


# The schema exactly as the pre-versioning Store.initialize() left it (user_version 0).
OLD_SCHEMA = """
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    feed_url TEXT NOT NULL UNIQUE,
    article_url TEXT,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
);
CREATE TABLE source_checks (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    candidates_seen INTEGER NOT NULL,
    error TEXT
);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sources_checked INTEGER NOT NULL,
    candidates_seen INTEGER NOT NULL,
    revisions_created INTEGER NOT NULL,
    failures INTEGER NOT NULL
);
CREATE TABLE article_revisions (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fetch_status TEXT NOT NULL
);
ALTER TABLE articles ADD COLUMN current_revision_id INTEGER REFERENCES article_revisions(id);
CREATE TABLE claims (
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
CREATE TABLE findings (
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
CREATE TABLE evidence (
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
CREATE TABLE topics (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    confidence TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE topic_articles (
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    revision_id INTEGER NOT NULL REFERENCES article_revisions(id),
    PRIMARY KEY (topic_id, revision_id)
);
CREATE TABLE revision_analysis (
    revision_id INTEGER PRIMARY KEY REFERENCES article_revisions(id),
    status TEXT NOT NULL CHECK (status = 'completed')
);
"""

OLD_HASH_ONE = article_content_hash("Old title", "Old body one.")
OLD_HASH_TWO = article_content_hash("Second title", "Second body.")
OLD_HASH_THREE = article_content_hash("Second title", "Second body, corrected.")

OLD_DATA = f"""
INSERT INTO sources VALUES (1, 'Alpha', 'https://alpha.test/feed', NULL, 1);
INSERT INTO sources VALUES (2, 'Bravo', 'https://bravo.test/feed', 'https://bravo.test/news', 0);
INSERT INTO source_checks VALUES (1, 1, '2026-09-18T04:00:00Z', 'ok', 2, NULL);
INSERT INTO source_checks VALUES (2, 2, '2026-09-18T04:00:00Z', 'failed', 0, 'Connection timed out');
INSERT INTO articles VALUES (1, 1, 'https://alpha.test/one', 'Old title', '2026-09-18T01:00:00Z', '2026-09-18T02:00:00Z',
                             '{{"category": "news"}}', NULL);
INSERT INTO articles VALUES (2, 2, 'https://bravo.test/two', 'Second title', NULL, '2026-09-18T02:30:00Z', '{{}}', NULL);
INSERT INTO article_revisions VALUES (1, 1, 'Old title', 'Old body one.', '2026-09-18T03:00:00Z', '{OLD_HASH_ONE}', 'ok');
INSERT INTO article_revisions VALUES (2, 2, 'Second title', 'Second body.', '2026-09-18T03:00:00Z', '{OLD_HASH_TWO}', 'ok');
INSERT INTO article_revisions VALUES (3, 2, 'Second title', 'Second body, corrected.', '2026-09-18T05:00:00Z',
                                      '{OLD_HASH_THREE}', 'ok');
UPDATE articles SET current_revision_id = 1 WHERE id = 1;
UPDATE articles SET current_revision_id = 3 WHERE id = 2;
INSERT INTO runs VALUES (1, '2026-09-18T04:00:00Z', '2026-09-18T04:01:00Z', 2, 2, 3, 1);
INSERT INTO claims VALUES (1, 1, 'Old body', 0, 8, 'checkable', 'material', 'extracted');
INSERT INTO findings VALUES (1, 1, 1, 'factual_contradiction', 'Old finding', 0, 8, 'resolved', 'retrieved', 1);
INSERT INTO evidence VALUES (1, 1, 'https://evidence.test/old', 'Old evidence', 'Old excerpt', 'contradicts', 'retrieved',
                             'direct', '2026-09-18T04:00:00Z', 'provider-a', NULL, 'hash-old');
INSERT INTO topics VALUES (1, 'Old topic', 'high', 'active');
INSERT INTO topic_articles VALUES (1, 1);
INSERT INTO topic_articles VALUES (1, 3);
INSERT INTO revision_analysis VALUES (1, 'completed');
"""


def build_old_database(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(OLD_SCHEMA + OLD_DATA)
    connection.close()
    return path


def schema_columns(path: Path) -> dict[str, set[str]]:
    connection = sqlite3.connect(path)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")} for table in tables}
    finally:
        connection.close()


def pragma(path: Path, name: str):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        connection.close()


def query(path: Path, sql: str, parameters: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def test_schema_version_counts_the_ordered_migrations():
    assert SCHEMA_VERSION == len(storage._MIGRATIONS) >= 4


def test_fresh_database_reaches_current_schema_with_all_new_tables_and_columns(tmp_path: Path):
    database = tmp_path / "fresh.db"
    Store(database).initialize()

    columns = schema_columns(database)
    assert pragma(database, "user_version") == SCHEMA_VERSION
    assert {
        "sources", "source_checks", "articles", "article_revisions", "runs", "claims", "findings", "evidence",
        "topics", "topic_articles", "revision_analysis", "llm_calls",
    } <= set(columns)
    assert {
        "revision_id", "status", "attempts", "last_error", "updated_at", "provider", "model", "prompt_version",
    } == columns["revision_analysis"]
    assert {"review_status", "reviewed_at", "review_note"} <= columns["findings"]
    assert "rationale" in columns["evidence"]
    assert {"etag", "last_modified", "last_fetched_at", "fetch_count", "unchanged_count", "current_revision_id"} <= columns["articles"]
    assert {"etag", "last_modified"} <= columns["sources"]
    assert {
        "id", "called_at", "provider", "model", "purpose", "revision_id", "status", "input_chars", "output_chars",
    } == columns["llm_calls"]
    assert "revision_analysis_new" not in columns
    assert "article_revisions_legacy" not in columns


def test_fresh_database_creates_lookup_indexes(tmp_path: Path):
    database = tmp_path / "fresh.db"
    Store(database).initialize()

    indexed = {
        (table, tuple(row[2] for row in query(database, f"PRAGMA index_info({name})")))
        for name, table in query(database, "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'")
    }
    assert {
        ("article_revisions", ("article_id",)),
        ("findings", ("revision_id",)),
        ("evidence", ("finding_id",)),
        ("source_checks", ("source_id", "checked_at")),
        ("llm_calls", ("called_at",)),
        ("topic_articles", ("revision_id",)),
    } <= indexed


def test_initialize_enables_wal_and_connections_set_foreign_keys_and_busy_timeout(tmp_path: Path):
    store = Store(tmp_path / "wal.db")
    store.initialize()

    assert pragma(store.path, "journal_mode") == "wal"
    for write in (True, False):
        with store._connection(write=write) as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
            assert connection.execute("PRAGMA busy_timeout").fetchone() == (5000,)


def test_readers_see_committed_state_while_another_process_holds_the_write_lock(tmp_path: Path):
    store = Store(tmp_path / "concurrent.db")
    store.initialize()
    store.add_source(Source(None, "Committed", "https://committed.test/feed"))
    writer = sqlite3.connect(store.path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO sources (name, feed_url, enabled) VALUES ('Uncommitted', 'https://pending.test/feed', 1)")

        assert [source.name for source in store.list_sources()] == ["Committed"]
        assert store.dashboard_snapshot()["sources"][0]["name"] == "Committed"
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_double_initialize_is_a_no_op(tmp_path: Path):
    store = Store(tmp_path / "twice.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))
    revision_id, _ = store.save_fetched_article(fetched(candidate(source_id)))
    store.mark_analysis_failed(revision_id, "2026-09-18T04:00:00Z", "timeout")
    before_schema = query(store.path, "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
    # data_version changes for a watching connection whenever another connection commits.
    watcher = sqlite3.connect(store.path)
    try:
        before_changes = watcher.execute("PRAGMA data_version").fetchone()[0]

        store.initialize()

        assert watcher.execute("PRAGMA data_version").fetchone()[0] == before_changes
        store.add_source(Source(None, "Control", "https://control.test/feed"))
        assert watcher.execute("PRAGMA data_version").fetchone()[0] != before_changes
    finally:
        watcher.close()
    assert query(store.path, "SELECT type, name, sql FROM sqlite_master ORDER BY type, name") == before_schema
    assert pragma(store.path, "user_version") == SCHEMA_VERSION
    assert store.analysis_status(revision_id)["attempts"] == 1


def test_old_schema_database_upgrades_without_data_loss(tmp_path: Path):
    database = build_old_database(tmp_path / "old.db")
    assert pragma(database, "user_version") == 0

    store = Store(database)
    store.initialize()

    assert pragma(database, "user_version") == SCHEMA_VERSION
    assert query(database, "PRAGMA foreign_key_check") == []
    assert query(database, "PRAGMA integrity_check") == [("ok",)]
    assert query(database, "SELECT id, name, feed_url, article_url, enabled, etag, last_modified FROM sources ORDER BY id") == [
        (1, "Alpha", "https://alpha.test/feed", None, 1, None, None),
        (2, "Bravo", "https://bravo.test/feed", "https://bravo.test/news", 0, None, None),
    ]
    assert query(database, "SELECT id, source_id, status, candidates_seen, error FROM source_checks ORDER BY id") == [
        (1, 1, "ok", 2, None),
        (2, 2, "failed", 0, "Connection timed out"),
    ]
    assert query(
        database,
        """SELECT id, source_id, url, title, published_at, discovered_at, metadata_json, current_revision_id,
                  etag, last_modified, last_fetched_at, fetch_count, unchanged_count
           FROM articles ORDER BY id""",
    ) == [
        (1, 1, "https://alpha.test/one", "Old title", "2026-09-18T01:00:00Z", "2026-09-18T02:00:00Z",
         '{"category": "news"}', 1, None, None, None, 0, 0),
        (2, 2, "https://bravo.test/two", "Second title", None, "2026-09-18T02:30:00Z", "{}", 3, None, None, None, 0, 0),
    ]
    assert query(database, "SELECT id, article_id, title, text, content_hash FROM article_revisions ORDER BY id") == [
        (1, 1, "Old title", "Old body one.", OLD_HASH_ONE),
        (2, 2, "Second title", "Second body.", OLD_HASH_TWO),
        (3, 2, "Second title", "Second body, corrected.", OLD_HASH_THREE),
    ]
    assert query(database, "SELECT * FROM runs") == [(1, "2026-09-18T04:00:00Z", "2026-09-18T04:01:00Z", 2, 2, 3, 1)]
    assert query(database, "SELECT id, revision_id, text, start, end FROM claims") == [(1, 1, "Old body", 0, 8)]
    assert query(
        database,
        "SELECT id, revision_id, claim_id, summary, status, visible, review_status, reviewed_at, review_note FROM findings",
    ) == [(1, 1, 1, "Old finding", "pending", 0, "unreviewed", None, None)]  # unassessed: hidden by migration 6
    assert query(database, "SELECT id, finding_id, url, excerpt, provider, content_hash, rationale FROM evidence") == [
        (1, 1, "https://evidence.test/old", "Old excerpt", "provider-a", "hash-old", None),
    ]
    assert query(database, "SELECT * FROM topics") == [(1, "Old topic", "high", "active")]
    assert query(database, "SELECT topic_id, revision_id FROM topic_articles ORDER BY revision_id") == [(1, 1), (1, 3)]
    assert store.analysis_status(1) == {
        "status": "completed", "attempts": 0, "last_error": None, "updated_at": None,
        "provider": None, "model": None, "prompt_version": None,
    }
    assert store.analysis_status(3) is None
    assert [revision.id for revision in store.list_pending_revisions(now="2026-09-18T06:00:00Z")] == [3]


def test_upgraded_old_database_accepts_every_new_job_status_and_feature(tmp_path: Path):
    store = Store(build_old_database(tmp_path / "old.db"))
    store.initialize()

    assert store.mark_analysis_running(3, "2026-09-18T06:00:00Z") is True
    assert store.mark_analysis_failed(3, "2026-09-18T06:01:00Z", "timeout") is True
    assert store.mark_analysis_skipped(3, "2026-09-18T06:02:00Z", "budget") is True
    assert store.analysis_status(3)["status"] == "skipped"
    assert store.review_finding(1, "confirmed", "2026-09-18T07:00:00Z", "Checked the record") is True
    assert store.record_llm_call("2026-09-18T06:00:00Z", "gemini", "gemini-2.5-flash", "claims", 3, "ok", 10, 5) == 1
    assert store.record_article_fetch("https://alpha.test/one", "2026-09-18T06:00:00Z", changed=False, etag='"v1"') is True
    with pytest.raises(sqlite3.IntegrityError):
        with store._connection() as connection:
            connection.execute("INSERT INTO revision_analysis (revision_id, status) VALUES (2, 'unknown')")


def test_fresh_and_upgraded_databases_share_the_same_tables_and_columns(tmp_path: Path):
    fresh = tmp_path / "fresh.db"
    Store(fresh).initialize()
    old = build_old_database(tmp_path / "old.db")
    Store(old).initialize()

    assert schema_columns(fresh) == schema_columns(old)


def test_legacy_unique_revision_index_database_upgrades_through_every_migration(tmp_path: Path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL, feed_url TEXT NOT NULL UNIQUE,
                                  article_url TEXT, enabled INTEGER NOT NULL);
            CREATE TABLE articles (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
                                   url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, published_at TEXT,
                                   discovered_at TEXT NOT NULL, metadata_json TEXT NOT NULL);
            CREATE TABLE article_revisions (id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL REFERENCES articles(id),
                                            text TEXT NOT NULL, fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL,
                                            fetch_status TEXT NOT NULL, UNIQUE(article_id, content_hash));
            INSERT INTO sources VALUES (1, 'Legacy', 'https://legacy.test/feed', NULL, 1);
            INSERT INTO articles VALUES (1, 1, 'https://legacy.test/article', 'Title', NULL, '2026-09-18T02:00:00Z', '{}');
            INSERT INTO article_revisions VALUES (1, 1, 'Body', '2026-09-18T03:00:00Z', 'legacy-hash', 'ok');
        """)
    connection.close()

    store = Store(database)
    store.initialize()

    assert pragma(database, "user_version") == SCHEMA_VERSION
    assert query(database, "SELECT current_revision_id FROM articles") == [(1,)]
    assert query(database, "PRAGMA foreign_key_check") == []
    references = {row[2] for table in ("claims", "findings", "topic_articles", "revision_analysis", "llm_calls")
                  for row in query(database, f"PRAGMA foreign_key_list({table})")}
    assert "article_revisions_legacy" not in references
    assert [revision.id for revision in store.list_pending_revisions(now="2026-09-18T06:00:00Z")] == [1]


def test_a_failing_migration_rolls_back_its_changes_and_keeps_the_version(tmp_path: Path, monkeypatch):
    database = tmp_path / "failing.db"
    Store(database).initialize()

    def broken_step(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE half_done (id INTEGER PRIMARY KEY)")
        connection.execute("ALTER TABLE sources ADD COLUMN half_column TEXT")
        raise RuntimeError("migration failed midway")

    monkeypatch.setattr(storage, "_MIGRATIONS", (*storage._MIGRATIONS, broken_step))
    monkeypatch.setattr(storage, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError, match="midway"):
        Store(database).initialize()

    assert pragma(database, "user_version") == SCHEMA_VERSION
    assert "half_done" not in schema_columns(database)
    assert "half_column" not in schema_columns(database)["sources"]


def test_a_failed_first_migration_leaves_an_old_database_untouched_for_retry(tmp_path: Path, monkeypatch):
    database = build_old_database(tmp_path / "old.db")
    original = storage._MIGRATIONS

    def fail_after_baseline(connection: sqlite3.Connection) -> None:
        raise RuntimeError("stop before the job-table rebuild")

    monkeypatch.setattr(storage, "_MIGRATIONS", (original[0], fail_after_baseline, *original[2:]))
    with pytest.raises(RuntimeError, match="job-table"):
        Store(database).initialize()

    assert pragma(database, "user_version") == 1
    assert "attempts" not in schema_columns(database)["revision_analysis"]

    monkeypatch.setattr(storage, "_MIGRATIONS", original)
    Store(database).initialize()
    assert pragma(database, "user_version") == SCHEMA_VERSION
    assert Store(database).analysis_status(1)["status"] == "completed"


def test_newer_schema_is_rejected_without_changes(tmp_path: Path):
    database = tmp_path / "newer.db"
    Store(database).initialize()
    connection = sqlite3.connect(database)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(SchemaVersionError, match="newer"):
        Store(database).initialize()
    assert issubclass(SchemaVersionError, sqlite3.Error)
    assert pragma(database, "user_version") == SCHEMA_VERSION + 1


def test_migrated_database_keeps_working_with_the_full_analysis_write_path(tmp_path: Path):
    store = Store(build_old_database(tmp_path / "old.db"))
    store.initialize()
    claim = Claim(None, 3, "Second body", 0, 11, "checkable", "material", "extracted")
    finding = Finding(None, 3, None, "factual_contradiction", "Check", 0, 11, "resolved", "retrieved", True)
    evidence = Evidence(
        None, None, "https://evidence.test/new?token=secret", "New", "Excerpt", "contradicts", "retrieved",
        "direct", "2026-09-18T06:00:00Z", rationale="The record says otherwise.",
    )

    store.save_analysis(3, (claim,), (finding,), ((evidence,),), completed_at="2026-09-18T06:05:00Z")
    topic_id = store.save_topic(TopicGroup(None, "New topic", "possible", "possible"))

    assert store.analysis_status(3)["status"] == "completed"
    assert store.list_topic_revisions(topic_id) == []
    assert query(store.path, "SELECT url, rationale FROM evidence WHERE id = 2") == [
        ("https://evidence.test/new?token=%2A%2A%2A", "The record says otherwise."),
    ]
