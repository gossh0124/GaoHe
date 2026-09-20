from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from gaohe.domain import ArticleRevision, Claim, Evidence, Finding, Source, TopicGroup, article_content_hash
from gaohe.storage import Store

from test_storage import candidate, fetched


def revision_store(tmp_path: Path) -> tuple[Store, int]:
    store = Store(tmp_path / "analysis.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))
    revision_id, _ = store.save_fetched_article(fetched(candidate(source_id), "Normalized article text."))
    return store, revision_id


def claim(revision_id: int, start: int = 0, end: int = 10) -> Claim:
    return Claim(None, revision_id, "Normalized", start, end, "checkable", "ordinary", "extracted")


def test_pending_revision_is_an_immutable_monitor_snapshot(tmp_path: Path):
    store, revision_id = revision_store(tmp_path)

    revisions = store.list_pending_revisions()

    assert revisions == [
        ArticleRevision(
            revision_id, 1, "https://example.test/articles/one", "Example article",
            "Normalized article text.", article_content_hash("Example article", "Normalized article text."),
            "2026-09-18T03:00:00Z",
        )
    ]


def test_claims_validate_spans_and_reject_duplicate_analysis_spans(tmp_path: Path):
    store, revision_id = revision_store(tmp_path)

    assert store.save_claims(revision_id, [claim(revision_id)]) == [1]
    with pytest.raises(ValueError, match="span"):
        store.save_claims(revision_id, [claim(revision_id, 11, 10)])
    with pytest.raises(ValueError, match="span"):
        store.save_claims(revision_id, [claim(revision_id, 0, 100)])
    with pytest.raises(ValueError, match="duplicate"):
        store.save_claims(revision_id, [claim(revision_id)])


def test_evidence_persists_success_and_failed_retrieval_attempts(tmp_path: Path):
    store, _ = revision_store(tmp_path)

    success_id = store.save_evidence(Evidence(None, None, "https://evidence.test/ok", "OK", "Full text", "supports", "retrieved", "direct", "2026-09-18T04:00:00Z"))
    failed_id = store.save_evidence(Evidence(None, None, "https://evidence.test/fail", "Fail", "", "context", "retrieval_failed", "direct", "2026-09-18T04:01:00Z"))

    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT status, retrieved_at, provider, published_at, content_hash FROM evidence ORDER BY id").fetchall()
    assert (success_id, failed_id) == (1, 2)
    assert rows == [
        ("retrieved", "2026-09-18T04:00:00Z", None, None, None),
        ("retrieval_failed", "2026-09-18T04:01:00Z", None, None, None),
    ]


def test_failed_evidence_replaces_untrusted_text_and_keeps_successful_evidence(tmp_path: Path):
    store, _ = revision_store(tmp_path)
    failed = Evidence(
        None, None, "https://user:pass@evidence.test/fail?api_key=secret", "Authorization: Bearer secret",
        "provider error Cookie: session=secret", "context", "retrieval_failed", "direct", "2026-09-18T04:01:00Z",
        "provider-a", "2026-09-17T04:01:00Z", "hash-a",
    )
    succeeded = Evidence(
        None, None, "https://evidence.test/ok", "Public title", "Public excerpt", "supports", "retrieved",
        "direct", "2026-09-18T04:00:00Z", "provider-a", "2026-09-17T04:00:00Z", "hash-ok",
    )

    store.save_evidence(failed)
    store.save_evidence(succeeded)

    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT url, title, excerpt, provider, published_at, content_hash FROM evidence ORDER BY id").fetchall()
    assert rows[0] == (
        "https://evidence.test/fail?api_key=%2A%2A%2A", "Evidence retrieval failed", "",
        "provider-a", "2026-09-17T04:01:00Z", "hash-a",
    )
    assert rows[1] == ("https://evidence.test/ok", "Public title", "Public excerpt", "provider-a", "2026-09-17T04:00:00Z", "hash-ok")


def test_findings_keep_visibility_independent_and_reject_unknown_types_or_statuses(tmp_path: Path):
    store, revision_id = revision_store(tmp_path)
    claim_id = store.save_claims(revision_id, [claim(revision_id)])[0]

    finding_id = store.save_finding(Finding(None, revision_id, claim_id, "factual_contradiction", "Needs evidence", 0, 10, "pending", "retrieval_failed", False))
    with pytest.raises(ValueError, match="finding_type"):
        store.save_finding(replace(Finding(None, revision_id, claim_id, "factual_contradiction", "Needs evidence", 0, 10, "pending", "retrieval_failed", False), finding_type="opinion"))
    with pytest.raises(ValueError, match="status"):
        store.save_finding(replace(Finding(None, revision_id, claim_id, "factual_contradiction", "Needs evidence", 0, 10, "pending", "retrieval_failed", False), status="unknown"))

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT visible, evidence_status FROM findings WHERE id = ?", (finding_id,)).fetchone() == (0, "retrieval_failed")


def test_topic_links_return_linked_revisions_and_initialization_preserves_records(tmp_path: Path):
    store, revision_id = revision_store(tmp_path)
    store.initialize()
    topic_id = store.save_topic(TopicGroup(None, "Example topic", "high", "active"))

    store.link_revision_to_topic(revision_id, topic_id)

    assert store.list_topic_revisions(topic_id)[0].id == revision_id
    store.initialize()
    assert store.list_topic_revisions(topic_id)[0].text == "Normalized article text."


def test_initialize_migrates_old_revisions_before_analysis_foreign_keys(tmp_path: Path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL, feed_url TEXT NOT NULL UNIQUE, article_url TEXT, enabled INTEGER NOT NULL);
            CREATE TABLE articles (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id), url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, published_at TEXT, discovered_at TEXT NOT NULL, metadata_json TEXT NOT NULL);
            CREATE TABLE article_revisions (id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL REFERENCES articles(id), text TEXT NOT NULL, fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL, fetch_status TEXT NOT NULL, UNIQUE(article_id, content_hash));
            CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, sources_checked INTEGER NOT NULL, candidates_seen INTEGER NOT NULL, revisions_created INTEGER NOT NULL, failures INTEGER NOT NULL);
            INSERT INTO sources VALUES (1, 'Legacy', 'https://legacy.test/feed', NULL, 1);
            INSERT INTO articles VALUES (1, 1, 'https://legacy.test/article', 'Legacy title', NULL, '2026-09-18T02:00:00Z', '{}');
            INSERT INTO article_revisions VALUES (1, 1, 'Legacy text', '2026-09-18T03:00:00Z', 'legacy-hash', 'ok');
        """)

    store = Store(database)
    store.initialize()
    store.initialize()
    claim_id = store.save_claims(1, [Claim(None, 1, "Legacy", 0, 6, "checkable", "ordinary", "extracted")])[0]
    finding_id = store.save_finding(Finding(None, 1, claim_id, "factual_contradiction", "Legacy finding", 0, 6, "pending", "pending", False))
    evidence_id = store.save_evidence(Evidence(None, finding_id, "https://evidence.test/legacy", "Legacy evidence", "Text", "context", "retrieved", "direct", "2026-09-18T04:00:00Z"))
    topic_id = store.save_topic(TopicGroup(None, "Legacy topic", "high", "active"))
    store.link_revision_to_topic(1, topic_id)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("SELECT title, text FROM article_revisions WHERE id = 1").fetchone() == ("Legacy title", "Legacy text")
        assert connection.execute("SELECT revision_id FROM claims WHERE id = ?", (claim_id,)).fetchone() == (1,)
        assert connection.execute("SELECT finding_id FROM evidence WHERE id = ?", (evidence_id,)).fetchone() == (finding_id,)
        assert connection.execute("SELECT revision_id FROM topic_articles WHERE topic_id = ?", (topic_id,)).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO claims (revision_id, text, start, end, kind, materiality, extraction_status) VALUES (999, 'x', 0, 1, 'checkable', 'ordinary', 'extracted')")


def test_revision_keeps_normalized_title_and_text_after_candidate_metadata_update(tmp_path: Path):
    store, revision_id = revision_store(tmp_path)
    source_id = store.list_sources()[0].id
    updated = candidate(source_id, "https://example.test/articles/one")
    store.save_candidate(replace(updated, title="New title", metadata={"marker": "new"}))

    revision = store.list_pending_revisions()[0]
    assert revision.title == "Example article"
    assert revision.text == "Normalized article text."


def test_revision_storage_normalizes_nfc_and_lf_for_hash_and_claim_spans(tmp_path: Path):
    store = Store(tmp_path / "normalized.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))
    item = candidate(source_id)
    raw_title = "Cafe\u0301\r\nTitle"
    raw_text = "Cafe\u0301\r\nBody"
    revision_id, _ = store.save_fetched_article(fetched(replace(item, title=raw_title), raw_text))

    revision = store.list_pending_revisions()[0]
    assert (revision.title, revision.text) == ("Café\nTitle", "Café\nBody")
    assert revision.content_hash == article_content_hash(raw_title, raw_text)
    assert store.save_claims(revision_id, [Claim(None, revision_id, "Body", 5, 9, "checkable", "ordinary", "extracted")])


def test_zero_claim_analysis_marks_revision_complete_but_failed_save_remains_pending(tmp_path: Path):
    store, revision_id = revision_store(tmp_path)

    with pytest.raises(ValueError, match="span"):
        store.save_claims(revision_id, [claim(revision_id, 20, 10)])
    assert [revision.id for revision in store.list_pending_revisions()] == [revision_id]
    assert store.save_claims(revision_id, []) == []
    assert store.list_pending_revisions() == []
