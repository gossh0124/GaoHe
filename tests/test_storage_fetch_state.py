from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from gaohe.domain import Source, article_content_hash
from gaohe.storage import Store

from test_storage import candidate, fetched


URL = "https://example.test/articles/one"


def source_store(tmp_path: Path) -> tuple[Store, int]:
    store = Store(tmp_path / "fetch.db")
    store.initialize()
    return store, store.add_source(Source(None, "Example", "https://example.test/feed"))


def article_row(store: Store) -> tuple:
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT source_id, title, published_at, discovered_at, metadata_json FROM articles WHERE url = ?", (URL,)
        ).fetchone()
    connection.close()
    return row


def test_upsert_keeps_first_seen_time_and_discovering_source(tmp_path: Path):
    store, source_id = source_store(tmp_path)
    other_source = store.add_source(Source(None, "Syndicator", "https://syndicator.test/feed"))
    store.save_candidate(candidate(source_id))

    store.save_candidate(replace(
        candidate(other_source), title="Updated title", published_at="2026-09-18T05:00:00Z",
        discovered_at="2026-09-19T08:00:00Z", metadata={"marker": "v2"},
    ))

    assert article_row(store) == (source_id, "Updated title", "2026-09-18T05:00:00Z", "2026-09-18T02:00:00Z", '{"marker": "v2"}')


def test_fetched_article_upsert_also_keeps_first_seen_time(tmp_path: Path):
    store, source_id = source_store(tmp_path)
    item = candidate(source_id)
    store.save_fetched_article(fetched(item))

    store.save_fetched_article(fetched(replace(item, discovered_at="2026-09-20T00:00:00Z"), "Changed body"))

    assert article_row(store)[3] == "2026-09-18T02:00:00Z"
    assert store.article_fetch_state(URL)["discovered_at"] == "2026-09-18T02:00:00Z"


def test_upsert_does_not_erase_a_known_publication_time(tmp_path: Path):
    store, source_id = source_store(tmp_path)
    store.save_candidate(candidate(source_id))

    store.save_candidate(replace(candidate(source_id), published_at=None))

    assert article_row(store)[2] == "2026-09-18T01:00:00Z"


def test_article_fetch_state_reports_defaults_and_current_revision_hash(tmp_path: Path):
    store, source_id = source_store(tmp_path)
    assert store.article_fetch_state(URL) is None

    store.save_candidate(candidate(source_id))
    assert store.article_fetch_state(URL) == {
        "etag": None, "last_modified": None, "last_fetched_at": None, "fetch_count": 0, "unchanged_count": 0,
        "published_at": "2026-09-18T01:00:00Z", "discovered_at": "2026-09-18T02:00:00Z", "content_hash": None,
    }

    store.save_fetched_article(fetched(candidate(source_id), "Second body"))
    assert store.article_fetch_state(URL)["content_hash"] == article_content_hash("Example article", "Second body")


def test_record_article_fetch_counts_changes_and_keeps_validators_unless_replaced(tmp_path: Path):
    store, source_id = source_store(tmp_path)
    store.save_fetched_article(fetched(candidate(source_id)))

    assert store.record_article_fetch(URL, "2026-09-18T04:00:00Z", changed=True, etag='W/"v1"',
                                      last_modified="Fri, 18 Sep 2026 03:59:00 GMT") is True
    assert store.record_article_fetch(URL, "2026-09-18T05:00:00Z", changed=False) is True
    assert store.record_article_fetch(URL, "2026-09-18T06:00:00+00:00", changed=False, etag='"v2"') is True

    state = store.article_fetch_state(URL)
    assert (state["etag"], state["last_modified"]) == ('"v2"', "Fri, 18 Sep 2026 03:59:00 GMT")
    assert (state["last_fetched_at"], state["fetch_count"], state["unchanged_count"]) == ("2026-09-18T06:00:00Z", 3, 2)

    store.record_article_fetch(URL, "2026-09-18T07:00:00Z", changed=True)
    state = store.article_fetch_state(URL)
    assert (state["fetch_count"], state["unchanged_count"], state["etag"]) == (4, 0, '"v2"')


@pytest.mark.parametrize(
    "bad_value",
    ['"v1"\r\nSet-Cookie: x=1', "\x00abc", "tab\tseparated", "line separator", "x" * 201, "", "   ",
     "token=abc123", 42],
)
def test_invalid_http_validators_are_dropped(tmp_path: Path, bad_value):
    store, source_id = source_store(tmp_path)
    store.save_fetched_article(fetched(candidate(source_id)))
    store.record_article_fetch(URL, "2026-09-18T04:00:00Z", changed=True, etag='"good"', last_modified="Fri, 18 Sep 2026 GMT")

    store.record_article_fetch(URL, "2026-09-18T05:00:00Z", changed=False, etag=bad_value, last_modified=bad_value)
    store.record_source_fetch_state(source_id, bad_value, bad_value)

    state = store.article_fetch_state(URL)
    assert (state["etag"], state["last_modified"]) == ('"good"', "Fri, 18 Sep 2026 GMT")
    assert state["fetch_count"] == 2
    assert store.source_fetch_state(source_id) == {"etag": None, "last_modified": None}


def test_http_validators_keep_the_200_character_boundary(tmp_path: Path):
    store, source_id = source_store(tmp_path)
    boundary = '"' + "a" * 198 + '"'

    store.record_source_fetch_state(source_id, boundary, None)

    assert store.source_fetch_state(source_id) == {"etag": boundary, "last_modified": None}


def test_record_article_fetch_reports_unknown_urls_and_validates_time(tmp_path: Path):
    store, source_id = source_store(tmp_path)
    store.save_fetched_article(fetched(candidate(source_id)))

    assert store.record_article_fetch("https://example.test/missing", "2026-09-18T04:00:00Z", changed=True) is False
    with pytest.raises(ValueError, match="UTC"):
        store.record_article_fetch(URL, "2026-09-18T04:00:00", changed=True)
    assert store.article_fetch_state(URL)["fetch_count"] == 0


def test_source_fetch_state_is_replaced_as_given(tmp_path: Path):
    store, source_id = source_store(tmp_path)
    assert store.source_fetch_state(source_id) == {"etag": None, "last_modified": None}
    assert store.source_fetch_state(999) is None

    assert store.record_source_fetch_state(source_id, '"feed-v1"', "Fri, 18 Sep 2026 03:00:00 GMT") is True
    assert store.source_fetch_state(source_id) == {"etag": '"feed-v1"', "last_modified": "Fri, 18 Sep 2026 03:00:00 GMT"}
    assert store.record_source_fetch_state(source_id, None, "Fri, 18 Sep 2026 04:00:00 GMT") is True
    assert store.source_fetch_state(source_id) == {"etag": None, "last_modified": "Fri, 18 Sep 2026 04:00:00 GMT"}
    assert store.record_source_fetch_state(999, '"x"', None) is False


def test_source_upsert_keeps_fetch_validators(tmp_path: Path):
    store, source_id = source_store(tmp_path)
    store.record_source_fetch_state(source_id, '"feed-v1"', None)

    assert store.add_source(Source(None, "Renamed", "https://example.test/feed")) == source_id

    assert store.source_fetch_state(source_id)["etag"] == '"feed-v1"'
