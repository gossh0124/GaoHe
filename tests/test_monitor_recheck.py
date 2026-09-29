from datetime import datetime, timedelta, timezone

import pytest

from gaohe.monitor import RECHECK_SCHEDULE, article_recheck_due


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
SECOND = timedelta(seconds=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def state(*, age=None, since_fetch=None, discovered_age=None, content_hash="hash"):
    """An Store.article_fetch_state row for an article of the given age, last fetched `since_fetch` ago."""
    return {
        "etag": None,
        "last_modified": None,
        "last_fetched_at": iso(NOW - since_fetch) if since_fetch is not None else None,
        "fetch_count": 1,
        "unchanged_count": 0,
        "published_at": iso(NOW - age) if age is not None else None,
        "discovered_at": iso(NOW - (discovered_age if discovered_age is not None else age or timedelta(0))),
        "content_hash": content_hash,
    }


def test_schedule_constant_matches_the_documented_bands():
    assert RECHECK_SCHEDULE == (
        (timedelta(hours=6), timedelta(0)),
        (timedelta(hours=24), timedelta(hours=3)),
        (timedelta(hours=72), timedelta(hours=12)),
        (timedelta(days=7), timedelta(hours=48)),
    )


def test_never_fetched_articles_are_always_due():
    assert article_recheck_due(None, NOW) is True
    # A listed article whose first fetch failed has a row but no revision yet.
    assert article_recheck_due(state(age=30 * DAY, since_fetch=HOUR, content_hash=None), NOW) is True
    assert article_recheck_due(state(age=30 * DAY, since_fetch=HOUR, content_hash=None), NOW, feed_marker_changed=False) is True


@pytest.mark.parametrize(
    ("age", "since_fetch", "due"),
    [
        # under 6 hours: every run, even straight after a fetch
        (timedelta(0), timedelta(0), True),
        (6 * HOUR - SECOND, timedelta(0), True),
        # 6 to 24 hours: at most every 3 hours
        (6 * HOUR, 3 * HOUR - SECOND, False),
        (6 * HOUR, 3 * HOUR, True),
        (24 * HOUR - SECOND, 3 * HOUR - SECOND, False),
        (24 * HOUR - SECOND, 3 * HOUR, True),
        # 24 to 72 hours: at most every 12 hours
        (24 * HOUR, 12 * HOUR - SECOND, False),
        (24 * HOUR, 12 * HOUR, True),
        (72 * HOUR - SECOND, 12 * HOUR - SECOND, False),
        (72 * HOUR - SECOND, 12 * HOUR, True),
        # 72 hours to 7 days: at most every 48 hours
        (72 * HOUR, 48 * HOUR - SECOND, False),
        (72 * HOUR, 48 * HOUR, True),
        (7 * DAY - SECOND, 48 * HOUR - SECOND, False),
        (7 * DAY - SECOND, 48 * HOUR, True),
        # 7 days or older: never rechecked by age
        (7 * DAY, 48 * HOUR, False),
        (7 * DAY, 30 * DAY, False),
        (90 * DAY, 90 * DAY, False),
    ],
)
def test_age_schedule_boundaries(age, since_fetch, due):
    assert article_recheck_due(state(age=age, since_fetch=since_fetch), NOW) is due


def test_a_changed_feed_marker_is_due_whatever_the_age():
    old = state(age=90 * DAY, since_fetch=timedelta(minutes=1))

    assert article_recheck_due(old, NOW, feed_marker_changed=True) is True


def test_an_unchanged_feed_marker_is_not_due_whatever_the_age():
    fresh = state(age=HOUR, since_fetch=HOUR)

    assert article_recheck_due(fresh, NOW) is True
    assert article_recheck_due(fresh, NOW, feed_marker_changed=False) is False


def test_discovered_at_stands_in_for_a_missing_published_at():
    recent = state(since_fetch=HOUR, discovered_age=2 * HOUR)
    older = state(since_fetch=HOUR, discovered_age=8 * DAY)

    assert article_recheck_due(recent, NOW) is True
    assert article_recheck_due(older, NOW) is False


def test_published_at_wins_over_discovered_at_unless_it_is_in_the_future():
    backfilled = state(age=8 * DAY, since_fetch=HOUR, discovered_age=HOUR)
    future_dated = {**state(since_fetch=HOUR, discovered_age=8 * DAY), "published_at": iso(NOW + DAY)}

    assert article_recheck_due(backfilled, NOW) is False
    assert article_recheck_due(future_dated, NOW) is False


def test_missing_or_future_fetch_time_is_due():
    legacy = state(age=10 * DAY)
    clock_went_back = {**state(age=10 * DAY), "last_fetched_at": iso(NOW + HOUR)}

    assert legacy["last_fetched_at"] is None
    assert article_recheck_due(legacy, NOW) is True
    assert article_recheck_due(clock_went_back, NOW) is True


def test_unparseable_dates_err_toward_fetching():
    unknown_age = {**state(since_fetch=HOUR), "published_at": "yesterday", "discovered_at": "not a date"}

    assert article_recheck_due(unknown_age, NOW) is True


def test_rfc822_and_offset_timestamps_are_understood():
    taipei = timezone(timedelta(hours=8))
    rfc822 = {**state(since_fetch=HOUR), "published_at": "Tue, 08 Sep 2026 12:00:00 +0000"}
    offset = {**state(since_fetch=2 * HOUR), "published_at": (NOW - 8 * HOUR).astimezone(taipei).isoformat()}

    assert article_recheck_due(rfc822, NOW) is False
    assert article_recheck_due(offset, NOW) is False
    assert article_recheck_due(offset, NOW + HOUR) is True
