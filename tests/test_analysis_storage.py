from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from gaohe.domain import ArticleRevision, Claim, Evidence, Finding, Source, article_content_hash
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
    with pytest.raises(sqlite3.IntegrityError):
        store.save_claims(revision_id, [claim(revision_id)])


def test_evidence_persists_success_and_failed_retrieval_attempts(tmp_path: Path):
    store, _ = revision_store(tmp_path)

    success_id = store.save_evidence(Evidence(None, None, "https://evidence.test/ok", "OK", "Full text", "supports", "retrieved", "direct", "2026-09-18T04:00:00Z"))
    failed_id = store.save_evidence(Evidence(None, None, "https://evidence.test/fail", "Fail", "", "context", "retrieval_failed", "direct", "2026-09-18T04:01:00Z"))

    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT status, retrieved_at FROM evidence ORDER BY id").fetchall()
    assert (success_id, failed_id) == (1, 2)
    assert rows == [("retrieved", "2026-09-18T04:00:00Z"), ("retrieval_failed", "2026-09-18T04:01:00Z")]


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
    with store._connection() as connection:
        topic_id = connection.execute("INSERT INTO topics (label, confidence, status) VALUES (?, ?, ?)", ("Example topic", "high", "active")).lastrowid

    store.link_revision_to_topic(revision_id, topic_id)

    assert store.list_topic_revisions(topic_id)[0].id == revision_id
    store.initialize()
    assert store.list_topic_revisions(topic_id)[0].text == "Normalized article text."
