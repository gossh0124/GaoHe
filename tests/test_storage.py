from pathlib import Path
import sqlite3

import pytest

from gaohe.domain import ArticleCandidate, FetchedArticle, RunSummary, Source, article_content_hash
from gaohe.storage import Store


def candidate(source_id: int, url: str = "https://example.test/articles/one") -> ArticleCandidate:
    return ArticleCandidate(
        source_id=source_id,
        url=url,
        title="Example article",
        published_at="2026-09-18T01:00:00Z",
        discovered_at="2026-09-18T02:00:00Z",
        metadata={"category": "news"},
    )


def fetched(item: ArticleCandidate, text: str = "First body") -> FetchedArticle:
    return FetchedArticle(
        candidate=item,
        text=text,
        fetched_at="2026-09-18T03:00:00Z",
        content_hash=article_content_hash(item.title, text),
    )


def test_initialize_creates_all_monitoring_tables(tmp_path: Path):
    database = tmp_path / "monitoring.db"
    Store(database).initialize()

    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert {"sources", "source_checks", "articles", "article_revisions", "runs"} <= tables


def test_source_is_upserted_by_feed_url_and_can_be_filtered(tmp_path: Path):
    store = Store(tmp_path / "monitoring.db")
    store.initialize()

    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))
    same_id = store.add_source(Source(None, "Updated", "https://example.test/feed", enabled=False))

    assert same_id == source_id
    assert store.list_sources() == [Source(source_id, "Updated", "https://example.test/feed", None, False)]
    assert store.list_sources(enabled_only=True) == []


def test_first_fetched_article_creates_a_revision(tmp_path: Path):
    store = Store(tmp_path / "monitoring.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))

    revision_id, created = store.save_fetched_article(fetched(candidate(source_id)))

    assert revision_id > 0
    assert created is True
    assert store.latest_content_hash("https://example.test/articles/one") == article_content_hash("Example article", "First body")


def test_unchanged_fetched_article_reuses_its_current_revision(tmp_path: Path):
    store = Store(tmp_path / "monitoring.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))
    item = candidate(source_id)

    first_id, _ = store.save_fetched_article(fetched(item))
    duplicate_id, created = store.save_fetched_article(fetched(item))

    assert duplicate_id == first_id
    assert created is False


def test_changed_fetched_article_creates_a_new_revision(tmp_path: Path):
    store = Store(tmp_path / "monitoring.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))
    item = candidate(source_id)

    first_id, _ = store.save_fetched_article(fetched(item))
    changed_id, created = store.save_fetched_article(fetched(item, "Changed body"))

    assert changed_id > first_id
    assert created is True
    assert store.latest_content_hash(item.url) == article_content_hash(item.title, "Changed body")


def test_reverted_content_creates_a_new_current_revision(tmp_path: Path):
    store = Store(tmp_path / "monitoring.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))
    item = candidate(source_id)

    first_id, _ = store.save_fetched_article(fetched(item, "A"))
    second_id, _ = store.save_fetched_article(fetched(item, "B"))
    third_id, created = store.save_fetched_article(fetched(item, "A"))

    with sqlite3.connect(tmp_path / "monitoring.db") as connection:
        revisions = connection.execute(
            "SELECT content_hash FROM article_revisions ORDER BY id"
        ).fetchall()

    assert [first_id, second_id, third_id] == sorted([first_id, second_id, third_id])
    assert created is True
    assert revisions == [
        (article_content_hash(item.title, "A"),),
        (article_content_hash(item.title, "B"),),
        (article_content_hash(item.title, "A"),),
    ]
    assert store.latest_content_hash(item.url) == article_content_hash(item.title, "A")


def test_foreign_keys_reject_unknown_source_and_keep_store_empty(tmp_path: Path):
    store = Store(tmp_path / "monitoring.db")
    store.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        store.save_fetched_article(fetched(candidate(999)))

    assert store.latest_content_hash("https://example.test/articles/one") is None


def test_failed_write_rolls_back_without_leaving_a_source(tmp_path: Path):
    store = Store(tmp_path / "monitoring.db")
    store.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        store.add_source(Source(None, None, "https://example.test/bad"))  # type: ignore[arg-type]

    assert store.list_sources() == []


def test_source_checks_and_runs_persist_bounded_safe_error_text(tmp_path: Path):
    store = Store(tmp_path / "monitoring.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))

    store.record_source_check(
        source_id,
        "2026-09-18T04:00:00Z",
        "failed",
        0,
        "Authorization: Bearer secret-value " + "x" * 600,
    )
    run_id = store.record_run(RunSummary("2026-09-18T04:00:00Z", None, 1, 0, 0, 1))

    with sqlite3.connect(tmp_path / "monitoring.db") as connection:
        error = connection.execute("SELECT error FROM source_checks").fetchone()[0]

    assert run_id > 0
    assert "secret-value" not in error
    assert len(error) <= 500


def test_source_check_errors_redact_cookie_token_and_query_secrets(tmp_path: Path):
    store = Store(tmp_path / "monitoring.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))

    store.record_source_check(
        source_id,
        "2026-09-18T04:00:00Z",
        "failed",
        0,
        "Cookie: session=private-cookie\nX-Token: private-token\n"
        "GET https://example.test/feed?access_token=query-token&client_secret=query-secret&password=query-password&kind=article",
    )
    store.record_source_check(source_id, "2026-09-18T05:00:00Z", "failed", 0, "Connection timed out after 5 seconds")

    with sqlite3.connect(tmp_path / "monitoring.db") as connection:
        errors = [row[0] for row in connection.execute("SELECT error FROM source_checks ORDER BY id")]

    assert all(value not in errors[0] for value in ["private-cookie", "private-token", "query-token", "query-secret", "query-password", "session="])
    assert "kind=article" in errors[0]
    assert errors[1] == "Connection timed out after 5 seconds"
