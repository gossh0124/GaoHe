import sqlite3

import pytest

from gaohe.domain import ArticleCandidate, Claim, Evidence, FetchedArticle, Finding, RunSummary, Source, article_content_hash
from gaohe.storage import IncompatibleDatabase, MANUAL_SOURCE_FEED_URL, SCHEMA_VERSION, Store

T0, T1, T2 = "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", "2026-10-01T02:00:00Z"


def candidate(source_id, url="https://news.example/a?token=SECRET", title="標題", published="2026-10-01T08:00:00+08:00", at=T0):
    return ArticleCandidate(source_id, url, title, published, at, {"discovery_type": "rss"})


def fetched(source_id, text="第一版內文", at=T0, **kwargs):
    item = candidate(source_id, at=at, **kwargs)
    return FetchedArticle(item, text, at, article_content_hash(item.title, text))


def test_initialize_creates_one_schema_is_idempotent_and_refuses_foreign_databases(tmp_path):
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    legacy = tmp_path / "old.db"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY)")
    with pytest.raises(IncompatibleDatabase) as error:
        Store(legacy).initialize()
    assert "gaohe.db" in str(error.value)


def test_sources_and_the_manual_source(store):
    feed_id = store.add_source(Source(None, "甲報", "https://a.example/rss"))
    manual_id = store.ensure_manual_source()
    assert store.ensure_manual_source() == manual_id
    assert [s.id for s in store.list_sources()] == [feed_id]
    manual = store.list_sources(include_manual=True)[-1]
    assert (manual.kind, manual.enabled, manual.feed_url) == ("manual", False, MANUAL_SOURCE_FEED_URL)
    assert not store.set_source_enabled(manual_id, True)  # the manual source is never polled
    assert store.set_source_enabled(feed_id, False) and store.list_sources(enabled_only=True) == []


def test_revisions_only_on_content_change_and_first_seen_facts_are_kept(store):
    first = store.add_source(Source(None, "甲報", "https://a.example/rss"))
    second = store.add_source(Source(None, "乙報", "https://b.example/rss"))
    revision_id, created = store.save_fetched_article(fetched(first))
    assert created
    assert store.save_fetched_article(fetched(second, at=T1, published=None)) == (revision_id, False)
    state = store.article_state("https://news.example/a?token=SECRET")
    assert state["last_fetched_at"] == T1 and state["discovered_at"] == T0
    assert state["published_at"] == T0  # +08:00 normalized to UTC and not erased by a later None
    new_id, created = store.save_fetched_article(fetched(second, "第二版內文", at=T2))
    assert created and new_id != revision_id
    assert [r.id for r in store.list_pending_revisions()] == [new_id]  # only the current revision is pending


def test_failed_jobs_retry_up_to_max_attempts_and_reset_allows_an_explicit_recheck(store):
    source = store.add_source(Source(None, "甲報", "https://a.example/rss"))
    revision_id, _ = store.save_fetched_article(fetched(source))
    for _ in range(3):
        store.mark_analysis_failed(revision_id, T1, "error with key=AIza" + "x" * 35)
    assert store.list_pending_revisions(max_attempts=3) == []
    status = store.analysis_status(revision_id)
    assert status["attempts"] == 3 and "AIza" not in status["last_error"]
    store.reset_analysis(revision_id)
    assert [r.id for r in store.list_pending_revisions(revision_ids=[revision_id])] == [revision_id]
    assert store.list_pending_revisions(revision_ids=[]) == []


def _analysis(store, revision_id):
    text = "第一版內文"
    claim = Claim(None, revision_id, text[0:3], 0, 3, "checkable", "material")
    visible = Finding(None, revision_id, "factual_contradiction", "說法不同", 0, 3, "retrieved", True)
    pending = Finding(None, revision_id, "unsupported_inference", "待查", 3, 5, "pending", False)
    proof = Evidence("https://gov.example/a?session=S1", "公告", "內文片段", "contradicts", "retrieved", T1, "search", "日期不同")
    lead = Evidence("https://lead.example/x", "lead", "should not be stored", "context", "retrieval_failed", None)
    return store.save_analysis(revision_id, [claim], [visible, pending], [[proof, lead], []], completed_at=T1, model="m", prompt_version="v")


def test_save_analysis_is_atomic_and_a_second_run_cannot_duplicate_results(store):
    source = store.add_source(Source(None, "甲報", "https://a.example/rss"))
    revision_id, _ = store.save_fetched_article(fetched(source))
    assert _analysis(store, revision_id) is True
    assert _analysis(store, revision_id) is False
    assert store.finding_counts(revision_id) == (1, 1)
    assert store.analysis_status(revision_id)["status"] == "completed"
    store.mark_analysis_failed(revision_id, T2, "late failure")  # never downgrades a completed job
    assert store.analysis_status(revision_id)["status"] == "completed"
    with pytest.raises(ValueError):
        store.save_analysis(revision_id + 99, [], [], [], completed_at=T1)


def test_dashboard_snapshot_shows_current_articles_visible_findings_and_redacts_urls(store):
    source = store.add_source(Source(None, "甲報", "https://a.example/rss"))
    revision_id, _ = store.save_fetched_article(fetched(source))
    _analysis(store, revision_id)
    store.record_source_check(source, T1, "failed", 3, "HTTP 500 for https://a.example/rss?api_key=K")
    store.record_run(RunSummary(T0, T1, 1, 3, 1, 1))
    snapshot = store.dashboard_snapshot()
    [item] = snapshot["inbox"]
    assert item["url"] == "https://news.example/a?token=%2A%2A%2A" and item["analysis_status"] == "completed"
    assert [f.finding_type for f in item["annotations"]] == ["factual_contradiction"] and item["pending_findings"] == 1
    assert [e["url"] for e in item["evidence"]] == ["https://gov.example/a?session=%2A%2A%2A", "https://lead.example/x"]
    assert item["evidence"][1]["excerpt"] == ""  # a failed lead never keeps text
    assert [f["summary"] for f in snapshot["findings"]] == ["說法不同"]
    assert snapshot["sources"][0]["status"] == "failed" and "K" not in snapshot["sources"][0]["error"].split("api_key=")[1]
    assert snapshot["last_run"]["revisions_created"] == 1
