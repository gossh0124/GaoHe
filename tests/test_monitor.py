from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeTransport, html_response, rss
from gaohe.config import Settings
from gaohe.domain import Source
from gaohe.monitor import MAX_ARTICLE_FETCHES_PER_SOURCE, MIN_HOST_DELAY_SECONDS, needs_fetch, parse_time, watch_once
from gaohe.sources import HttpResponse

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
FEED = "https://a.example/rss"


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def feed_response(*items, url=FEED):
    return HttpResponse(200, url, {"Content-Type": "application/rss+xml"}, rss(*items))


def watch(store, transport, now=NOW, sleeps=None):
    # A frozen clock: every same-host request must wait the full delay (recorded, never slept).
    return watch_once(Settings(), store, transport, now, clock=lambda: 0.0, sleep=(sleeps if sleeps is not None else []).append)


@pytest.mark.parametrize("state, expected", [
    (None, True),
    ({"content_hash": None}, True),
    ({"content_hash": "h", "published_at": iso(NOW - timedelta(hours=10)), "last_fetched_at": iso(NOW - timedelta(hours=4))}, True),
    ({"content_hash": "h", "published_at": iso(NOW - timedelta(hours=10)), "last_fetched_at": iso(NOW - timedelta(hours=1))}, False),
    ({"content_hash": "h", "published_at": iso(NOW - timedelta(days=3)), "last_fetched_at": iso(NOW - timedelta(days=1))}, False),
    ({"content_hash": "h", "discovered_at": iso(NOW - timedelta(hours=5)), "last_fetched_at": iso(NOW - timedelta(hours=3))}, True),
])
def test_needs_fetch_rule(state, expected):
    assert needs_fetch(state, NOW) is expected


def test_parse_time_reads_taiwan_cst_and_rejects_naive_values():
    assert parse_time("Wed, 01 Oct 2026 20:00:00 CST") == NOW
    assert parse_time("2026-10-01T20:00:00+08:00") == NOW
    assert parse_time("2026-10-01T12:00:00") is None and parse_time("soon") is None


def test_new_articles_are_fetched_once_and_rechecked_only_while_fresh(store):
    store.add_source(Source(None, "甲報", FEED))
    article = "https://a.example/news/1"
    transport = FakeTransport({
        FEED: feed_response(("開放觀光", article, "Wed, 01 Oct 2026 19:00:00 +0800")),
        article: html_response(article, "開放觀光", "第一版"),
    })
    first = watch(store, transport)
    assert (first.sources_checked, first.candidates_seen, first.revisions_created, first.failures) == (1, 1, 1, 0)
    assert watch(store, transport, NOW + timedelta(hours=1)).revisions_created == 0
    assert transport.calls.count(article) == 1  # not due an hour later
    transport.pages[article] = html_response(article, "開放觀光", "更正後的第二版")
    assert watch(store, transport, NOW + timedelta(hours=4)).revisions_created == 1
    assert watch(store, transport, NOW + timedelta(days=3)).revisions_created == 0
    assert transport.calls.count(article) == 2


def test_per_source_cap_host_delay_and_cross_source_dedupe(store):
    store.add_source(Source(None, "甲報", FEED))
    store.add_source(Source(None, "乙報", "https://b.example/rss"))
    links = [f"https://a.example/n/{index}" for index in range(MAX_ARTICLE_FETCHES_PER_SOURCE + 5)]
    pages = {link: html_response(link, "t", f"內文 {link}") for link in links}
    pages[FEED] = feed_response(*[("t", link, None) for link in links])
    pages["https://b.example/rss"] = feed_response(("t", links[0], None), url="https://b.example/rss")
    sleeps = []
    summary = watch(store, FakeTransport(pages), sleeps=sleeps)
    assert summary.revisions_created == MAX_ARTICLE_FETCHES_PER_SOURCE
    assert sleeps and set(sleeps) == {MIN_HOST_DELAY_SECONDS}
    assert summary.candidates_seen == len(links) + 1


def test_failures_are_recorded_per_source_and_an_empty_feed_is_fine(store):
    broken = store.add_source(Source(None, "壞掉", "https://broken.example/rss"))
    store.add_source(Source(None, "空的", FEED))
    transport = FakeTransport({"https://broken.example/rss": OSError("refused"), FEED: feed_response()})
    summary = watch(store, transport)
    assert (summary.sources_checked, summary.failures) == (2, 1)
    statuses = {row["id"]: row["status"] for row in store.dashboard_snapshot()["sources"]}
    assert statuses[broken] == "failed" and list(statuses.values()).count("ok") == 1
