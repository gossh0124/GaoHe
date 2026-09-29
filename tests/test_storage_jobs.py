from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from gaohe.domain import ANALYSIS_STATUSES, ArticleCandidate, Claim, Evidence, FetchedArticle, Finding, Source, article_content_hash
from gaohe.storage import Store


NOW = "2026-09-18T12:00:00Z"


def article(source_id: int, index: int, text: str, fetched_at: str = "2026-09-18T03:00:00Z") -> FetchedArticle:
    item = ArticleCandidate(source_id, f"https://news.test/{index}", f"Story {index}", None, "2026-09-18T02:00:00Z", {})
    return FetchedArticle(item, text, fetched_at, article_content_hash(item.title, text))


def store_with_articles(tmp_path: Path, count: int = 3) -> tuple[Store, list[int]]:
    store = Store(tmp_path / "jobs.db")
    store.initialize()
    source_id = store.add_source(Source(None, "News", "https://news.test/feed"))
    revision_ids = [store.save_fetched_article(article(source_id, index, f"Body {index} text."))[0] for index in range(count)]
    return store, revision_ids


def pending_ids(store: Store, **options) -> list[int]:
    options.setdefault("now", NOW)
    return [revision.id for revision in store.list_pending_revisions(**options)]


def set_job_row(store: Store, revision_id: int, status: str, attempts: int = 0, updated_at: str | None = None) -> None:
    with store._connection() as connection:
        connection.execute(
            """INSERT INTO revision_analysis (revision_id, status, attempts, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(revision_id) DO UPDATE SET status = excluded.status, attempts = excluded.attempts,
                 updated_at = excluded.updated_at""",
            (revision_id, status, attempts, updated_at),
        )


def test_missing_pending_and_skipped_jobs_are_listed_in_revision_order(tmp_path: Path):
    store, (first, second, third) = store_with_articles(tmp_path)
    set_job_row(store, second, "pending")
    store.mark_analysis_skipped(third, "2026-09-18T04:00:00Z", "daily budget reached")

    assert pending_ids(store) == [first, second, third]
    assert pending_ids(store, limit=2) == [first, second]


def test_completed_jobs_are_not_listed(tmp_path: Path):
    store, (first, second, _) = store_with_articles(tmp_path)

    store.save_analysis(first, (), (), (), completed_at="2026-09-18T04:00:00Z")

    assert first not in pending_ids(store)
    assert pending_ids(store)[0] == second


def test_superseded_revisions_are_never_listed(tmp_path: Path):
    store = Store(tmp_path / "jobs.db")
    store.initialize()
    source_id = store.add_source(Source(None, "News", "https://news.test/feed"))
    old_id, _ = store.save_fetched_article(article(source_id, 1, "Original body."))
    store.mark_analysis_failed(old_id, "2026-09-18T04:00:00Z", "timeout")
    new_id, created = store.save_fetched_article(article(source_id, 1, "Corrected body.", "2026-09-18T05:00:00Z"))

    assert created is True
    assert pending_ids(store) == [new_id]
    store.save_analysis(new_id, (), (), (), completed_at="2026-09-18T06:00:00Z")
    assert pending_ids(store) == []


def test_failed_jobs_are_retried_until_max_attempts(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)

    for attempt in range(1, 4):
        assert revision_id in pending_ids(store)
        store.mark_analysis_running(revision_id, "2026-09-18T04:00:00Z")
        store.mark_analysis_failed(revision_id, "2026-09-18T04:01:00Z", f"timeout {attempt}")
        assert store.analysis_status(revision_id)["attempts"] == attempt

    assert pending_ids(store) == []
    assert pending_ids(store, max_attempts=4) == [revision_id]
    assert pending_ids(store, max_attempts=1) == []


def test_fresh_running_jobs_are_hidden_and_stale_ones_are_recovered(tmp_path: Path):
    store, (fresh, stale, boundary) = store_with_articles(tmp_path)
    store.mark_analysis_running(fresh, "2026-09-18T11:30:00Z")
    store.mark_analysis_running(stale, "2026-09-18T10:59:59.500000Z")
    store.mark_analysis_running(boundary, "2026-09-18T11:00:00Z")

    assert pending_ids(store) == [stale]
    assert pending_ids(store, stale_after_minutes=20) == [fresh, stale, boundary]
    assert pending_ids(store, now="2026-09-18T11:10:00Z") == []


def test_running_job_without_parseable_timestamp_is_treated_as_stale(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)
    set_job_row(store, revision_id, "running", 0, None)

    assert pending_ids(store) == [revision_id]


def test_now_accepts_aware_utc_datetime_and_rejects_other_clocks(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)
    store.mark_analysis_running(revision_id, "2026-09-18T10:00:00Z")

    assert pending_ids(store, now=datetime(2026, 9, 18, 12, tzinfo=timezone.utc)) == [revision_id]
    assert pending_ids(store, now=datetime(2026, 9, 18, 10, 30, tzinfo=timezone.utc)) == []
    with pytest.raises(ValueError, match="UTC"):
        store.list_pending_revisions(now=datetime(2026, 9, 18, 12))
    with pytest.raises(ValueError, match="UTC"):
        store.list_pending_revisions(now=datetime(2026, 9, 18, 20, tzinfo=timezone(timedelta(hours=8))))
    with pytest.raises(ValueError, match="UTC"):
        store.list_pending_revisions(now="2026-09-18T20:00:00+08:00")


@pytest.mark.parametrize("options", [{"limit": 0}, {"max_attempts": 0}, {"stale_after_minutes": 0}])
def test_list_pending_rejects_non_positive_bounds(tmp_path: Path, options: dict):
    store, _ = store_with_articles(tmp_path, 1)

    with pytest.raises(ValueError, match="positive"):
        store.list_pending_revisions(now=NOW, **options)


def test_reclaiming_an_abandoned_running_job_counts_one_attempt(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)

    for crash in range(1, 4):
        assert pending_ids(store, now="2026-09-19T00:00:00Z") == [revision_id]
        store.mark_analysis_running(revision_id, "2026-09-18T04:00:00Z")
        assert store.analysis_status(revision_id)["attempts"] == crash - 1

    assert store.analysis_status(revision_id)["last_error"] == "previous analysis run did not finish"
    store.mark_analysis_running(revision_id, "2026-09-18T04:00:00Z")
    assert store.analysis_status(revision_id)["attempts"] == 3
    assert pending_ids(store, now="2026-09-19T00:00:00Z") == []


def test_job_transitions_record_timestamps_attempts_and_redacted_errors(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)

    assert store.analysis_status(revision_id) is None
    assert store.mark_analysis_running(revision_id, "2026-09-18T04:00:00+00:00") is True
    assert store.analysis_status(revision_id) == {
        "status": "running", "attempts": 0, "last_error": None, "updated_at": "2026-09-18T04:00:00Z",
        "provider": None, "model": None, "prompt_version": None,
    }
    secret_error = "HTTP 401 Authorization: Bearer sk-live-secret\nGET https://api.test/v1?key=1&api_key=AIzaSECRET " + "x" * 900
    assert store.mark_analysis_failed(revision_id, "2026-09-18T04:01:00Z", secret_error) is True
    status = store.analysis_status(revision_id)
    assert (status["status"], status["attempts"], status["updated_at"]) == ("failed", 1, "2026-09-18T04:01:00Z")
    assert "sk-live-secret" not in status["last_error"]
    assert "AIzaSECRET" not in status["last_error"]
    assert len(status["last_error"]) <= 500
    assert store.mark_analysis_skipped(revision_id, "2026-09-18T04:02:00Z", "daily budget reached") is True
    assert store.analysis_status(revision_id) | {"updated_at": None} == {
        "status": "skipped", "attempts": 1, "last_error": "daily budget reached", "updated_at": None,
        "provider": None, "model": None, "prompt_version": None,
    }


def test_failed_job_accepts_exception_objects_and_missing_errors(tmp_path: Path):
    store, (first, second, _) = store_with_articles(tmp_path)

    store.mark_analysis_failed(first, "2026-09-18T04:00:00Z", RuntimeError("token=abc123 rejected"))  # type: ignore[arg-type]
    store.mark_analysis_failed(second, "2026-09-18T04:00:00Z", None)

    assert store.analysis_status(first)["last_error"] == "token=[redacted] rejected"
    assert store.analysis_status(second)["last_error"] is None


def test_completed_job_cannot_be_moved_back_by_a_late_worker(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)
    store.save_analysis(revision_id, (), (), (), completed_at="2026-09-18T04:00:00Z")
    completed = store.analysis_status(revision_id)

    assert store.mark_analysis_running(revision_id, "2026-09-18T05:00:00Z") is False
    assert store.mark_analysis_failed(revision_id, "2026-09-18T05:00:00Z", "late failure") is False
    assert store.mark_analysis_skipped(revision_id, "2026-09-18T05:00:00Z", "late skip") is False
    assert store.analysis_status(revision_id) == completed


def test_job_transitions_validate_revision_and_timestamp(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)

    for mark in (store.mark_analysis_failed, store.mark_analysis_skipped):
        with pytest.raises(ValueError, match="unknown revision"):
            mark(999, "2026-09-18T04:00:00Z", "reason")
        with pytest.raises(ValueError, match="UTC"):
            mark(revision_id, "2026-09-18T04:00:00", "reason")
    with pytest.raises(ValueError, match="unknown revision"):
        store.mark_analysis_running(999, "2026-09-18T04:00:00Z")
    with pytest.raises(ValueError):
        store.mark_analysis_running(revision_id, "not a time")
    assert store.analysis_status(revision_id) is None


def test_save_analysis_completes_job_with_provider_metadata_and_keeps_attempts(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)
    store.mark_analysis_running(revision_id, "2026-09-18T04:00:00Z")
    store.mark_analysis_failed(revision_id, "2026-09-18T04:01:00Z", "timeout")
    store.mark_analysis_running(revision_id, "2026-09-18T05:00:00Z")
    claim = Claim(None, revision_id, "Body", 0, 4, "checkable", "ordinary", "extracted")

    store.save_analysis(
        revision_id, (claim,), (), (), provider="gemini", model="gemini-2.5-flash",
        prompt_version="claims-v2", completed_at="2026-09-18T05:02:00+00:00",
    )

    assert store.analysis_status(revision_id) == {
        "status": "completed", "attempts": 1, "last_error": None, "updated_at": "2026-09-18T05:02:00Z",
        "provider": "gemini", "model": "gemini-2.5-flash", "prompt_version": "claims-v2",
    }


def test_save_analysis_drops_unsafe_provider_labels(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)

    store.save_analysis(
        revision_id, (), (), (), provider="Authorization: Bearer secret", model="key=AIzaSECRET",
        prompt_version="v1\nsecret", completed_at="2026-09-18T05:00:00Z",
    )

    status = store.analysis_status(revision_id)
    assert (status["provider"], status["model"], status["prompt_version"]) == (None, None, None)
    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())
    connection.close()
    assert "secret" not in dump.lower()
    assert "AIzaSECRET" not in dump


def test_save_analysis_rejects_invalid_completion_time_before_writing(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)
    claim = Claim(None, revision_id, "Body", 0, 4, "checkable", "ordinary", "extracted")

    with pytest.raises(ValueError, match="UTC"):
        store.save_analysis(revision_id, (claim,), (), (), completed_at="2026-09-18T05:00:00")

    assert store.analysis_status(revision_id) is None
    assert store.save_claims(revision_id, [claim]) == [1]


def test_failed_save_analysis_leaves_the_running_job_untouched(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)
    store.mark_analysis_running(revision_id, "2026-09-18T04:00:00Z")
    finding = Finding(None, revision_id, None, "factual_contradiction", "Check", 0, 4, "pending", "pending", False)
    invalid = Evidence(None, None, "https://evidence.test/a", "A", "", "context", "pending", "invalid", None)

    with pytest.raises(ValueError, match="source_kind"):
        store.save_analysis(revision_id, (), (finding,), ((invalid,),), completed_at="2026-09-18T04:05:00Z")

    assert store.analysis_status(revision_id)["status"] == "running"
    assert store.analysis_status(revision_id)["updated_at"] == "2026-09-18T04:00:00Z"


def test_save_claims_still_completes_the_job_with_a_timestamp(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)

    store.save_claims(revision_id, [])

    status = store.analysis_status(revision_id)
    assert status["status"] == "completed"
    assert datetime.fromisoformat(status["updated_at"].replace("Z", "+00:00")).tzinfo is not None
    assert pending_ids(store) == []


def test_analysis_counts_cover_every_status_for_current_revisions_only(tmp_path: Path):
    store, (first, second, third) = store_with_articles(tmp_path)
    source_id = store.list_sources()[0].id
    store.save_candidate(ArticleCandidate(source_id, "https://news.test/no-body", "Listed only", None, "2026-09-18T02:00:00Z", {}))

    assert store.analysis_counts() == {status: 0 for status in ANALYSIS_STATUSES} | {"unanalyzed": 3}

    store.save_analysis(first, (), (), (), completed_at="2026-09-18T04:00:00Z")
    store.mark_analysis_failed(second, "2026-09-18T04:00:00Z", "timeout")
    newer_second, _ = store.save_fetched_article(article(source_id, 1, "Body 1 corrected.", "2026-09-18T05:00:00Z"))
    store.mark_analysis_running(newer_second, "2026-09-18T05:01:00Z")
    store.mark_analysis_skipped(third, "2026-09-18T04:00:00Z", "budget")

    assert store.analysis_counts() == {
        "pending": 0, "running": 1, "completed": 1, "failed": 0, "skipped": 1, "unanalyzed": 0,
    }


def test_llm_ledger_records_sanitized_labels_and_counts_calls_since(tmp_path: Path):
    store, (revision_id, *_) = store_with_articles(tmp_path, 1)

    first = store.record_llm_call("2026-09-18T04:00:00Z", "gemini", "gemini-2.5-flash", "claim_extraction", revision_id, "ok", 1200, 300)
    second = store.record_llm_call("2026-09-18T04:00:00.500000Z", "Bearer sk-secret", "key=AIza", "assessment", None, "error")
    store.record_llm_call("2026-09-18T05:00:00+00:00", None, None, "assessment", revision_id, "rate_limited", 10, 0)

    assert (first, second) == (1, 2)
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            "SELECT called_at, provider, model, purpose, revision_id, status, input_chars, output_chars FROM llm_calls ORDER BY id"
        ).fetchall()
    connection.close()
    assert rows == [
        ("2026-09-18T04:00:00Z", "gemini", "gemini-2.5-flash", "claim_extraction", revision_id, "ok", 1200, 300),
        ("2026-09-18T04:00:00.500000Z", None, None, "assessment", None, "error", 0, 0),
        ("2026-09-18T05:00:00Z", None, None, "assessment", revision_id, "rate_limited", 10, 0),
    ]
    assert store.count_llm_calls_since("2026-09-18T00:00:00Z") == 3
    assert store.count_llm_calls_since("2026-09-18T04:00:00Z") == 3
    assert store.count_llm_calls_since("2026-09-18T04:00:00.100000Z") == 2
    assert store.count_llm_calls_since("2026-09-18T04:00:01Z") == 1
    assert store.count_llm_calls_since("2026-09-18T05:00:00.000001Z") == 0


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("2026-09-18T04:00:00Z", "gemini", "m", "claim extraction", None, "ok"), "purpose"),
        (("2026-09-18T04:00:00Z", "gemini", "m", "", None, "ok"), "purpose"),
        (("2026-09-18T04:00:00Z", "gemini", "m", "claims", None, "Authorization: Bearer x"), "status"),
        (("2026-09-18T04:00:00Z", "gemini", "m", "claims", None, "ok", -1, 0), "negative"),
        (("2026-09-18T04:00:00Z", "gemini", "m", "claims", 999, "ok"), "unknown revision"),
        (("2026-09-18T12:00:00+08:00", "gemini", "m", "claims", None, "ok"), "UTC"),
    ],
)
def test_llm_ledger_rejects_invalid_calls_without_writing(tmp_path: Path, arguments: tuple, message: str):
    store, _ = store_with_articles(tmp_path, 1)

    with pytest.raises(ValueError, match=message):
        store.record_llm_call(*arguments)

    assert store.count_llm_calls_since("2000-01-01T00:00:00Z") == 0


def test_count_llm_calls_since_validates_timestamp(tmp_path: Path):
    store, _ = store_with_articles(tmp_path, 1)

    with pytest.raises(ValueError, match="UTC"):
        store.count_llm_calls_since("2026-09-18T00:00:00")
